"""股票翻倍計畫：掃描器主程式。

用法（在這個資料夾裡執行）：
  python scan.py                   自動判斷：美股盤中跑「盤中暫定」，其他時間跑「收盤確認」
  python scan.py --mode intraday   強制盤中暫定
  python scan.py --mode close      強制收盤確認（會記入歷史，用於汰弱留強的連續天數）
  python scan.py --backfill 5      第一次安裝時用：補算過去 5 個交易日的收盤分數
  python scan.py --demo            不連網，用模擬資料跑一次（測試用）

輸出 docs/data.json，只含代號、分數與訊號，不含任何金額。
"""

import argparse
import hashlib
import json
import os
import sys
import tomllib
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import indicators as ind

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")  # 自動處理夏令時間
HISTORY_PATH = HERE / "state" / "history.json"
PORTFOLIO_PATH = HERE / "state" / "portfolio.json"
HISTORY_KEEP_DAYS = 60
MIN_BARS = 130

SIGNAL_LABELS = {
    "strong": "強勢續抱",
    "hold": "續抱",
    "reduce": "轉弱留意",
    "exit": "出場訊號",
    "swap_out": "汰換對象",
    "buy": "可進場",
    "watch": "觀察",
    "none": "未達標",
    "swap_in": "換股候選",
}


# ───────── 設定 ─────────

def load_config():
    with open(HERE / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    themes = {}
    for theme, tickers in cfg.get("candidates", {}).items():
        for t in tickers:
            themes.setdefault(t.upper(), theme)
    holdings = [t.upper() for t in cfg["holdings"]["tickers"]]
    for t, theme in cfg.get("holding_themes", {}).items():
        themes[t.upper()] = theme
    pool = list(dict.fromkeys(
        t.upper() for ts in cfg.get("candidates", {}).values() for t in ts
        if t.upper() not in holdings))
    w = cfg.get("weights", {})
    return {
        "benchmark": cfg.get("benchmark", "SPY").upper(),
        "output_path": HERE / cfg.get("output_path", "docs/data.json"),
        "swap_gap": float(cfg.get("swap_gap", 15)),
        "swap_days": int(cfg.get("swap_days", 3)),
        "weights": {
            "relative_strength": w.get("relative_strength", 30),
            "money_flow": w.get("money_flow", 25),
            "trend": w.get("trend", 20),
            "rebound": w.get("rebound", 15),
            "risk": w.get("risk", 10),
        },
        "fixed": holdings,  # 你自己指定的持股，程式不會自動換掉
        "pool": pool,
        "target_count": int(cfg["holdings"].get("target_count", 16)),
        "max_per_theme": int(cfg["holdings"].get("max_per_theme", 4)),
        "holdings": list(holdings),
        "candidates": list(pool),
        "themes": themes,
    }


# ───────── 自動選股組合 ─────────
# 持股 = config 裡你自己寫的代號 + 程式自動挑的代號（存在 state/portfolio.json）。
# 名額不足 target_count 時，從候選池挑分數最高的補滿，每個主題最多 max_per_theme 檔。
# 換股確認後，若最弱的是自動挑的那檔，程式會直接換掉；若是你自己指定的，只發訊號。

def load_portfolio():
    try:
        with open(PORTFOLIO_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"auto": [], "changes": []}


def apply_portfolio(cfg, port):
    port["auto"] = [t for t in port["auto"] if t in cfg["pool"]]  # 你從候選池刪掉的就移除
    cfg["holdings"] = cfg["fixed"] + port["auto"]
    cfg["candidates"] = [t for t in cfg["pool"] if t not in cfg["holdings"]]


def theme_counts(cfg):
    counts = {}
    for t in cfg["holdings"]:
        th = cfg["themes"].get(t, "")
        counts[th] = counts.get(th, 0) + 1
    return counts


def fill_portfolio(cfg, port, results, date):
    counts = theme_counts(cfg)
    ranked = sorted((t for t in cfg["candidates"] if t in results),
                    key=lambda t: -results[t]["score"])
    added = []
    for t in ranked:
        if len(cfg["holdings"]) + len(added) >= cfg["target_count"]:
            break
        th = cfg["themes"].get(t, "")
        if counts.get(th, 0) >= cfg["max_per_theme"]:
            continue
        counts[th] = counts.get(th, 0) + 1
        added.append(t)
        port["auto"].append(t)
        port["changes"].append({"date": date, "action": "add", "ticker": t,
                                "score": results[t]["score"]})
    apply_portfolio(cfg, port)
    return added


def execute_swap(cfg, port, swaps, results, date):
    """收盤確認的換股：只換自動挑的持股，一天最多換一檔。"""
    ready = sorted((s for s in swaps.items()
                    if s[1]["confirmed_streak"] >= cfg["swap_days"]),
                   key=lambda s: -(s[1]["gap"] or 0))
    if not ready:
        return None
    out = ready[0][1]["out"]
    if out not in port["auto"]:
        return None
    counts = theme_counts(cfg)
    counts[cfg["themes"].get(out, "")] -= 1
    for cand, s in ready:
        th = cfg["themes"].get(cand, "")
        if counts.get(th, 0) >= cfg["max_per_theme"]:
            continue
        port["auto"][port["auto"].index(out)] = cand
        change = {"date": date, "action": "swap", "out": out, "in": cand,
                  "gap": s["gap"], "streak": s["confirmed_streak"]}
        port["changes"].append(change)
        apply_portfolio(cfg, port)
        return change
    return None


# ───────── 時間與模式 ─────────

def market_session(now_et):
    """open = 美東平日 9:30-16:00；其他時間 closed。國定假日由資料日期判斷。"""
    if now_et.weekday() < 5 and time(9, 30) <= now_et.time() < time(16, 0):
        return "open"
    return "closed"


def expected_close_date(now_et):
    """最近一個「已經收盤」的平日（不管國定假日，假日由資料判斷）。"""
    d = pd.Timestamp(now_et.date())
    if now_et.weekday() >= 5 or now_et.time() < time(16, 0):
        d -= pd.offsets.BDay(1)
    return d.date().isoformat()


def date_lt(d, iso):
    return d.isoformat() < iso


def nothing_to_do(now_et, hist):
    """自動模式下，休市時段且最近一次收盤已確認，就不必再抓資料。

    排程可以全天每 15 分鐘呼叫，程式自己決定要不要跑；
    收盤後到 18:00 會重跑幾次，讓 Yahoo 的收盤資料定稿。
    """
    if market_session(now_et) == "open":
        return False
    if now_et.weekday() < 5 and time(16, 0) <= now_et.time() < time(18, 0):
        return False
    exp = expected_close_date(now_et)
    return exp in hist["confirmed"] or hist.get("holiday_checked") == exp


# ───────── 資料 ─────────

def _split_download(raw, tickers):
    out = {}
    if raw is None or raw.empty:
        return out
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        for t in tickers:
            if t in level0:
                out[t] = raw[t]
    elif len(tickers) == 1:
        out[tickers[0]] = raw
    clean = {}
    for t, df in out.items():
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        if len(df):
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            clean[t] = df
    return clean


def fetch_yahoo(tickers):
    import yfinance as yf
    kw = dict(period="2y", interval="1d", auto_adjust=True,
              group_by="ticker", progress=False, threads=True)
    data = _split_download(yf.download(tickers, **kw), tickers)
    missing = [t for t in tickers if t not in data]
    for t in missing:  # 失敗的逐檔再試一次
        try:
            data.update(_split_download(yf.download([t], **kw), [t]))
        except Exception:
            pass
    return data


def fetch_demo(tickers, end=None):
    """模擬資料：每檔固定亂數種子，結果可重現。"""
    end = pd.Timestamp(end or datetime.now(ET).date())
    idx = pd.bdate_range(end=end, periods=320)
    data = {}
    for t in tickers:
        rng = np.random.default_rng(int(hashlib.md5(t.encode()).hexdigest()[:8], 16))
        drift = rng.normal(0.0006, 0.0012)
        vol = rng.uniform(0.015, 0.05)
        r = rng.normal(drift, vol, len(idx))
        close = 50 * np.exp(np.cumsum(r))
        spread = np.abs(rng.normal(0, vol, len(idx))) * close
        opn = close * (1 - r / 2)
        df = pd.DataFrame({
            "Open": opn,
            "High": np.maximum(opn, close) + spread / 2,
            "Low": np.minimum(opn, close) - spread / 2,
            "Close": close,
            "Volume": rng.integers(1_000_000, 20_000_000, len(idx)).astype(float),
        }, index=idx)
        data[t] = df
    return data


# ───────── 評分 ─────────

def score_universe(data, cfg, asof=None):
    """回傳 {ticker: {...}}，所有分項與總分。asof 可截斷到某一天（補算用）。"""
    bench = data.get(cfg["benchmark"])
    if bench is None:
        raise RuntimeError(f"抓不到大盤基準 {cfg['benchmark']} 的資料")
    if asof is not None:
        bench = bench[bench.index <= asof]
    universe = cfg["holdings"] + cfg["candidates"]
    frames, errors = {}, []
    for t in universe:
        df = data.get(t)
        if df is None:
            errors.append({"ticker": t, "error": "抓不到資料"})
            continue
        if asof is not None:
            df = df[df.index <= asof]
        if len(df) < MIN_BARS:
            errors.append({"ticker": t, "error": f"歷史資料不足（{len(df)} 天）"})
            continue
        frames[t] = df

    rs_raw = {t: ind.rs_raw(df["Close"], bench["Close"]) for t, df in frames.items()}
    rs_pct = ind.percentile_rank(rs_raw) if rs_raw else {}
    w = cfg["weights"]
    pos_total = w["relative_strength"] + w["money_flow"] + w["trend"] + w["rebound"]

    results = {}
    for t, df in frames.items():
        comp = {
            "rs": rs_pct[t],
            "mf": ind.money_flow_score(df),
            "trend": ind.trend_score(df),
            "rebound": ind.rebound_score(df),
            "risk": ind.risk_score(df),
        }
        # 正向四項加權後縮放到 (100 - risk 權重)，再加上 risk 權重 x (1 - 風險)
        positive = (w["relative_strength"] * comp["rs"] + w["money_flow"] * comp["mf"]
                    + w["trend"] * comp["trend"] + w["rebound"] * comp["rebound"]) / pos_total
        score = positive * (100 - w["risk"]) / 100 + w["risk"] * (100 - comp["risk"]) / 100
        c = df["Close"]
        results[t] = {
            "score": round(float(score), 1),
            "components": {k: round(float(v), 1) for k, v in comp.items()},
            "chg_1d_pct": round(float(c.iloc[-1] / c.iloc[-2] - 1) * 100, 2),
            "above_sma20": bool(c.iloc[-1] > ind.sma(c, 20).iloc[-1]),
            "above_sma50": bool(c.iloc[-1] > ind.sma(c, 50).iloc[-1]),
            "data_date": df.index[-1].date().isoformat(),
        }
    return results, errors


def reasons_for(r):
    c = r["components"]
    out = []
    if c["rs"] >= 70:
        out.append("相對大盤強勢")
    elif c["rs"] <= 30:
        out.append("相對大盤弱勢")
    if c["mf"] >= 65:
        out.append("資金流入")
    elif c["mf"] <= 35:
        out.append("資金流出")
    if c["trend"] >= 80:
        out.append("多頭排列")
    elif c["trend"] <= 20:
        out.append("空頭排列")
    if c["rebound"] >= 60:
        out.append("拉回止跌")
    if c["risk"] >= 70:
        out.append("波動大")
    if not r["above_sma50"]:
        out.append("跌破 50 日線")
    return out


def holding_signal(r):
    if r["score"] < 40 and not r["above_sma50"]:
        return "exit"
    if r["score"] < 50:
        return "reduce"
    if r["score"] >= 75:
        return "strong"
    return "hold"


def candidate_signal(r):
    if r["score"] >= 70 and r["components"]["trend"] >= 60:
        return "buy"
    if r["score"] >= 60:
        return "watch"
    return "none"


# ───────── 歷史與汰弱留強 ─────────

def load_history():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"confirmed": {}}


def save_history(hist):
    days = sorted(hist["confirmed"])
    for d in days[:-HISTORY_KEEP_DAYS]:
        del hist["confirmed"][d]
    write_json_atomic(HISTORY_PATH, hist)


def record_confirmed(hist, date, results, holdings):
    hist["confirmed"][date] = {
        "scores": {t: r["score"] for t, r in results.items()},
        "holdings": [t for t in holdings if t in results],
    }


def swap_analysis(hist, cfg, today=None, only=None):
    """today = (date, scores, holdings) 的盤中暫定資料，或 None。

    only：只拿這些持股比（例如只比自動挑的持股，用來決定要不要自動換股）。
    回傳 {candidate: {"streak": n, "confirmed_streak": m, "gap": x, "out": weakest}}。
    """
    days = [(d, v["scores"], v["holdings"]) for d, v in sorted(hist["confirmed"].items())]
    if today is not None and (not days or days[-1][0] != today[0]):
        days.append(today)
        has_provisional = True
    else:
        has_provisional = False

    def gap_on(day, cand):
        _, scores, holds = day
        hs = [scores[h] for h in holds if h in scores and (only is None or h in only)]
        if cand not in scores or not hs:
            return None
        return scores[cand] - min(hs)

    def streak(seq, cand):
        n = 0
        for day in reversed(seq):
            g = gap_on(day, cand)
            if g is None or g < cfg["swap_gap"]:
                break
            n += 1
        return n

    if not days:
        return {}, None
    _, last_scores, last_holds = days[-1]
    held = [h for h in last_holds if h in last_scores and (only is None or h in only)]
    weakest = min(held, key=lambda h: last_scores[h]) if held else None
    out = {}
    for cand in cfg["candidates"]:
        if cand not in last_scores:
            continue
        g = gap_on(days[-1], cand)
        out[cand] = {
            "gap": round(g, 1) if g is not None else None,
            "streak": streak(days, cand),
            "confirmed_streak": streak(days[:-1] if has_provisional else days, cand),
            "out": weakest,
        }
    return out, weakest


# ───────── 輸出 ─────────

def write_json_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def build_output(cfg, results, errors, mode, session, now_utc, swaps, weakest, demo, port):
    need = cfg["swap_days"]

    def swap_status(s):
        if s["streak"] >= need:
            return "confirmed" if s["confirmed_streak"] >= need else "provisional"
        return "building" if s["streak"] > 0 else None

    holdings, candidates, swap_list = [], [], []
    for t in cfg["holdings"]:
        if t not in results:
            continue
        r = results[t]
        sig = holding_signal(r)
        holdings.append({"ticker": t, "theme": cfg["themes"].get(t, ""), **r,
                         "signal": sig, "signal_label": SIGNAL_LABELS[sig],
                         "reasons": reasons_for(r), "weakest": t == weakest,
                         "source": "manual" if t in cfg["fixed"] else "auto"})
    for t in cfg["candidates"]:
        if t not in results:
            continue
        r = results[t]
        sig = candidate_signal(r)
        s = swaps.get(t)
        status = swap_status(s) if s else None
        if status in ("confirmed", "provisional"):
            sig = "swap_in"
            swap_list.append({"in": t, "out": s["out"], "gap": s["gap"],
                              "streak": s["streak"], "need": need, "status": status})
        candidates.append({"ticker": t, "theme": cfg["themes"].get(t, ""), **r,
                           "signal": sig, "signal_label": SIGNAL_LABELS[sig],
                           "reasons": reasons_for(r),
                           "swap": ({"status": status, "streak": s["streak"], "need": need,
                                     "gap": s["gap"], "out": s["out"]} if status else None)})
    holdings.sort(key=lambda x: -x["score"])
    candidates.sort(key=lambda x: -x["score"])
    swap_list.sort(key=lambda x: -(x["gap"] or 0))
    if any(s["status"] == "confirmed" for s in swap_list):
        for h in holdings:
            if h["ticker"] == weakest:
                h["signal"], h["signal_label"] = "swap_out", SIGNAL_LABELS["swap_out"]

    data_date = max((r["data_date"] for r in results.values()), default=None)
    return {
        "version": 1,
        "demo": demo,
        "mode": mode,  # intraday = 盤中暫定；close = 收盤確認
        "mode_label": "盤中暫定" if mode == "intraday" else "收盤確認",
        "market_session": session,
        "generated_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "generated_at_et": now_utc.astimezone(ET).strftime("%Y-%m-%d %H:%M ET"),
        "data_date": data_date,
        # 網頁用：超過這個分鐘數沒更新就顯示「資料過期」
        "stale_after_minutes": 40 if mode == "intraday" else 60 * 30,
        "benchmark": cfg["benchmark"],
        "weights": cfg["weights"],
        "swap_rule": {"gap": cfg["swap_gap"], "days": need},
        "weakest_holding": weakest,
        "swaps": swap_list,
        # 自動組合的異動紀錄（新增、換股），最新的在前
        "portfolio_changes": list(reversed(port["changes"][-30:])),
        "holdings": holdings,
        "candidates": candidates,
        "errors": errors,
    }


# ───────── 主程式 ─────────

def main():
    ap = argparse.ArgumentParser(description="股票翻倍計畫掃描器")
    ap.add_argument("--mode", choices=["auto", "intraday", "close"], default="auto")
    ap.add_argument("--backfill", type=int, default=0, metavar="N",
                    help="補算過去 N 個交易日的收盤分數（第一次安裝用）")
    ap.add_argument("--demo", action="store_true", help="用模擬資料，不連網")
    args = ap.parse_args()

    global HISTORY_PATH, PORTFOLIO_PATH
    if args.demo:  # 模擬資料的狀態另外存，不會汙染真的紀錄
        HISTORY_PATH = HERE / "state" / "history_demo.json"
        PORTFOLIO_PATH = HERE / "state" / "portfolio_demo.json"

    cfg = load_config()
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ET)
    session = market_session(now_et)
    hist = load_history()
    if args.mode == "auto" and not args.demo and not args.backfill and nothing_to_do(now_et, hist):
        print("休市中，最近一次收盤已確認，這次不更新。")
        return 0

    tickers = list(dict.fromkeys([cfg["benchmark"]] + cfg["holdings"] + cfg["candidates"]))
    data = fetch_demo(tickers) if args.demo else fetch_yahoo(tickers)
    if cfg["benchmark"] not in data:
        print(f"錯誤：抓不到 {cfg['benchmark']}，可能是網路問題，這次不更新。", file=sys.stderr)
        return 1
    bench_last = data[cfg["benchmark"]].index[-1].date()

    mode = args.mode
    if mode == "auto":
        # 盤中時段但最新資料不是今天 = 休市（國定假日），改跑收盤確認
        mode = "intraday" if session == "open" and bench_last == now_et.date() else "close"
    if session == "open" and bench_last != now_et.date():
        session = "holiday"

    if mode == "close" and date_lt(bench_last, expected_close_date(now_et)):
        hist["holiday_checked"] = expected_close_date(now_et)  # 資料沒有該日 = 國定假日
    port = load_portfolio()
    apply_portfolio(cfg, port)

    results, errors = score_universe(data, cfg)
    date = bench_last.isoformat()
    added = fill_portfolio(cfg, port, results, date)
    if added:
        print(f"自動選入 {len(added)} 檔：{', '.join(added)}")

    if args.backfill:
        dates = list(data[cfg["benchmark"]].index[-(args.backfill + 1):-1])
        for d in dates:
            res, _ = score_universe(data, cfg, asof=d)
            record_confirmed(hist, d.date().isoformat(), res, cfg["holdings"])
            print(f"補算 {d.date()}：{len(res)} 檔")

    if mode == "close":
        record_confirmed(hist, date, results, cfg["holdings"])
        swaps, weakest = swap_analysis(hist, cfg)
        auto_swaps, _ = swap_analysis(hist, cfg, only=set(port["auto"]))
        change = execute_swap(cfg, port, auto_swaps, results, date)
        if change:
            print(f"自動換股：{change['out']} → {change['in']}（差 {change['gap']} 分）")
            # 收盤後已換股，今天的紀錄改成新組合，訊號也重算
            hist["confirmed"][date]["holdings"] = [t for t in cfg["holdings"] if t in results]
            swaps, weakest = swap_analysis(hist, cfg)
    else:
        today = (date, {t: r["score"] for t, r in results.items()},
                 [t for t in cfg["holdings"] if t in results])
        swaps, weakest = swap_analysis(hist, cfg, today=today)
    if mode == "close" or args.backfill:
        save_history(hist)
    write_json_atomic(PORTFOLIO_PATH, port)

    out = build_output(cfg, results, errors, mode, session, now_utc, swaps, weakest,
                       args.demo, port)
    write_json_atomic(cfg["output_path"], out)
    print(f"{out['mode_label']}｜資料日期 {date}｜持股 {len(out['holdings'])} 檔，"
          f"候選 {len(out['candidates'])} 檔｜換股訊號 {len(out['swaps'])} 個｜"
          f"失敗 {len(errors)} 檔 → {cfg['output_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
