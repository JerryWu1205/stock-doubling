"""選股方式與回測。

三種選股方式共用同一套換股規則（候選股比最弱持股高 swap_gap 分、連續 swap_days 個收盤日才換）：
  momentum  強勢股：分數高就買（最早的做法）
  pullback  強勢股等回檔：分數高、趨勢向上，而且拉回到 20 / 50 日線附近、開始止跌才買，不追高
  dip       跌深反彈：買過去半年跌深、超賣、開始止跌的股票

這裡把每個分項算成「整段時間序列」，一次就能算出過去一年每天的分數，拿來回測。
回測假設：每個收盤確認日依規則換股，持股等權重，空位放現金；不含手續費、稅和滑價。
"""

import numpy as np
import pandas as pd

import indicators as ind

STRATEGIES = {
    "momentum": "強勢股",
    "pullback": "強勢股等回檔",
    "dip": "跌深反彈",
}


def _scale(x, lo, hi):
    return ((x - lo) / (hi - lo) * 100).clip(0, 100)


def _rolling_mdd(c, n=63):
    def mdd(w):
        return float((1 - w / np.maximum.accumulate(w)).max())
    return c.rolling(n, min_periods=n).apply(mdd, raw=True)


def component_series(df, bench_close):
    """單一股票（還原日線）每一天的各分項，0-100。rs_raw 還要在全體中排百分位。"""
    c, h, l = df["Close"], df["High"], df["Low"]
    b = bench_close.reindex(c.index).ffill()
    out = pd.DataFrame(index=c.index)

    def ret(s, n):
        return s / s.shift(n) - 1

    out["rs_raw"] = (0.3 * (ret(c, 21) - ret(b, 21)) + 0.4 * (ret(c, 63) - ret(b, 63))
                     + 0.3 * (ret(c, 126) - ret(b, 126)))

    mfi = ind.mfi(df)
    cmf = ind.cmf(df)
    up = (c > c.shift(1)).astype(float)
    vol = df["Volume"]
    up_ratio = (vol * up).rolling(20).sum() / vol.rolling(20).sum().replace(0, np.nan)
    out["mf"] = (0.5 * _scale(mfi, 20, 80) + 0.25 * _scale(cmf.fillna(0), -0.25, 0.25)
                 + 0.25 * _scale(up_ratio.fillna(0.5), 0.3, 0.7))

    s5, s20, s50, s200 = ind.sma(c, 5), ind.sma(c, 20), ind.sma(c, 50), ind.sma(c, 200)
    has200 = s200.notna()
    checks = [
        c > s20,
        c > s50,
        np.where(has200, c > s200, c > s50),
        np.where(has200, s50 > s200, s20 > s50),
        s20 > s20.shift(5),
    ]
    out["trend"] = 20.0 * sum(pd.Series(np.asarray(x, dtype=bool), index=c.index).astype(float)
                              for x in checks)

    hi63 = c.rolling(63, min_periods=1).max()
    dd = 1 - c / hi63
    depth = np.where(dd < 0.05, dd / 0.05 * 20,
                     np.where(dd <= 0.20, 40, np.maximum(0, 40 - (dd - 0.20) / 0.15 * 40)))
    rsi = ind.rsi(c)
    rsi_turn = ((rsi.shift(1).rolling(5).min() < 40) & (rsi > rsi.shift(1))) * 30.0
    rng = h - l
    candle = ((c > c.shift(1)) & (rng > 0) & ((c - l) / rng.replace(0, np.nan) >= 0.5)) * 15.0
    rising = (s200 > s200.shift(20)) | s200.shift(20).isna()
    support = (((c / s50 - 1).abs() <= 0.04) & rising) * 15.0
    out["rebound"] = (pd.Series(depth, index=c.index) + rsi_turn + candle + support).clip(0, 100)

    vola = c.pct_change().rolling(20).std() * np.sqrt(252)
    out["risk"] = 0.5 * _scale(vola, 0.20, 1.20) + 0.5 * _scale(_rolling_mdd(c), 0.05, 0.50)

    # 跌深反彈的分項
    dd126 = 1 - c / c.rolling(126, min_periods=20).max()
    out["dip_depth"] = np.where(dd126 < 0.10, dd126 / 0.10 * 50,
                                np.where(dd126 <= 0.35, 100,
                                         np.maximum(0, 100 - (dd126 - 0.35) / 0.20 * 100)))
    out["dip_oversold"] = _scale(40 - rsi.rolling(10).min(), 0, 15)
    out["dip_reversal"] = (40.0 * (c > c.rolling(5).min() * 1.03) + 30.0 * (rsi > rsi.shift(3))
                           + 30.0 * (c > s5))
    out["dip_mfturn"] = np.where(mfi < 60, _scale(mfi - mfi.shift(5), 0, 20), 0)

    # 強勢股等回檔：有沒有拉回、有沒有止跌、是不是漲太遠
    out["pulled"] = (c <= s20 * 1.03) | (c <= s50 * 1.05)
    out["intact"] = c >= s50 * 0.97
    out["turning"] = (c > c.shift(1)) | (rsi > rsi.shift(1))
    out["extended"] = (c > s20 * 1.08) | (rsi > 70)
    out["dd126"] = dd126
    out["new_low"] = c < l.rolling(20).min().shift(1)
    return out


def strategy_score(row_or_df, strategy, weights):
    """momentum / pullback 用原本的評分；dip 用跌深反彈的評分。風險一律最多扣 weights['risk'] 分。"""
    x = row_or_df
    w = weights
    if strategy == "dip":
        positive = (35 * x["dip_depth"] + 25 * x["dip_oversold"] + 25 * x["dip_reversal"]
                    + 15 * x["dip_mfturn"]) / 100
    else:
        pos_total = w["relative_strength"] + w["money_flow"] + w["trend"] + w["rebound"]
        positive = (w["relative_strength"] * x["rs"] + w["money_flow"] * x["mf"]
                    + w["trend"] * x["trend"] + w["rebound"] * x["rebound"]) / pos_total
    return positive * (100 - w["risk"]) / 100 + w["risk"] * (100 - x["risk"]) / 100


def entry_ok(x, strategy, score):
    """這一天是否符合「可進場」。momentum 只看分數和趨勢。"""
    if strategy == "dip":
        return (score >= 60) & (x["dip_reversal"] >= 70)
    strong = (score >= 70) & (x["trend"] >= 60)
    if strategy == "pullback":
        return strong & x["pulled"].astype(bool) & x["intact"].astype(bool) & x["turning"].astype(bool)
    return strong


def panel(frames, bench_close):
    """所有股票的分項時間序列 → {分項: DataFrame(日期 x 代號)}，rs 已排好百分位。"""
    per = {t: component_series(df, bench_close) for t, df in frames.items()}
    cols = next(iter(per.values())).columns if per else []
    out = {k: pd.DataFrame({t: s[k] for t, s in per.items()}) for k in cols}
    out["rs"] = out["rs_raw"].rank(axis=1, pct=True) * 100
    out["rs"] = out["rs"].where(out["rs_raw"].notna(), 50.0)
    return out


def run_backtest(frames, bench_close, cfg, strategy, rules, days=250):
    """依換股規則逐日模擬（rules = scan 模組，用同一套選股和換股函式）。回傳 {curve: [[日期, 淨值]], ...}。"""
    fill_portfolio, execute_swap = rules.fill_portfolio, rules.execute_swap
    swap_analysis, apply_portfolio = rules.swap_analysis, rules.apply_portfolio

    p = panel(frames, bench_close)
    w = cfg["weights"]
    score = strategy_score(p, strategy, w)
    ok = entry_ok(p, strategy, score)
    closes = pd.DataFrame({t: df["Close"] for t, df in frames.items()})
    rets = closes.pct_change()
    dates = [d for d in bench_close.index if d in score.index][-(days + 1):]
    if len(dates) < 30:
        return None

    c = dict(cfg)
    c["holdings"], c["candidates"] = list(cfg["fixed"]), list(cfg["pool"])
    port = {"auto": [], "changes": []}
    apply_portfolio(c, port)
    hist = {"confirmed": {}}
    gate = strategy != "momentum"
    value, curve, swaps_n = 1.0, [], 0
    held_prev = []
    for d in dates:
        if held_prev:  # 前一天收盤的持股，吃到今天的報酬
            r = rets.loc[d, held_prev].fillna(0.0)
            slots = max(c["target_count"], len(held_prev))
            value *= 1 + float(r.sum()) / slots
        curve.append([d.date().isoformat(), round(value, 4)])

        sc = score.loc[d]
        okd = ok.loc[d]
        results = {t: {"score": float(sc[t]), "entry_ok": bool(okd[t])} for t in sc.index
                   if np.isfinite(sc[t])}
        if not results:
            continue
        day = d.date().isoformat()
        fill_portfolio(c, port, results, day, gate=gate)
        hist["confirmed"][day] = {"scores": {t: r["score"] for t, r in results.items()},
                                  "holdings": [t for t in c["holdings"] if t in results]}
        for old in sorted(hist["confirmed"])[:-8]:
            del hist["confirmed"][old]
        auto_swaps, _ = swap_analysis(hist, c, only=set(port["auto"]))
        if execute_swap(c, port, auto_swaps, results, day, gate=gate):
            swaps_n += 1
            hist["confirmed"][day]["holdings"] = [t for t in c["holdings"] if t in results]
            fill_portfolio(c, port, results, day, gate=gate)
        held_prev = [t for t in c["holdings"] if t in closes.columns]

    vals = np.array([v for _, v in curve])
    peak = np.maximum.accumulate(vals)
    return {"label": STRATEGIES[strategy], "curve": curve,
            "total_return_pct": round((vals[-1] - 1) * 100, 1),
            "max_drawdown_pct": round(float((1 - vals / peak).max()) * 100, 1),
            "swaps": swaps_n,
            "final_holdings": list(c["holdings"])}


def bench_curve(bench_close, dates):
    b = bench_close.reindex(pd.to_datetime(dates))
    b = b / b.iloc[0]
    vals = b.to_numpy(float)
    peak = np.maximum.accumulate(vals)
    return {"curve": [[d, round(float(v), 4)] for d, v in zip(dates, vals)],
            "total_return_pct": round((vals[-1] - 1) * 100, 1),
            "max_drawdown_pct": round(float((1 - vals / peak).max()) * 100, 1)}


def live_curve(hist, frames, bench_close):
    """實際紀錄：每個收盤確認日的持股（等權重），吃到下一個確認日的報酬。"""
    days = sorted(hist["confirmed"])
    if len(days) < 2:
        return None
    closes = pd.DataFrame({t: df["Close"] for t, df in frames.items()})
    value, curve = 1.0, [[days[0], 1.0]]
    for a, b in zip(days, days[1:]):
        held = [t for t in hist["confirmed"][a]["holdings"] if t in closes.columns]
        ta, tb = pd.Timestamp(a), pd.Timestamp(b)
        if held and ta in closes.index and tb in closes.index:
            r = closes.loc[tb, held] / closes.loc[ta, held] - 1
            value *= 1 + float(r.fillna(0).mean())
        curve.append([b, round(value, 4)])
    bench = bench_curve(bench_close, [d for d, _ in curve])
    return {"curve": curve, "total_return_pct": round((value - 1) * 100, 1), "benchmark": bench}
