"""期權資料：美股個股選擇權（Yahoo）、台指選擇權與外資台指期（期交所 OpenAPI）。

只供網頁顯示，不進評分。先記錄下來，之後用訊號成績單驗證有沒有預測力，有才考慮放進分數。
輸出：docs/data/options/<市場>.json；失敗只記在 logs/options_probe.json。
"""

import traceback
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

import fundamentals as fu

HERE = fu.HERE
DATA_DIR = fu.DATA_DIR
STATE = fu.STATE
PROBE = HERE / "logs" / "options_probe.json"
TAIFEX = "https://openapi.taifex.com.tw/v1"
_probe = {}


def _iso(now_utc):
    return now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def settlement_dates(key, n=3):
    """接下來 n 個月結算日：美股每月第三個週五，台股每月第三個週三。"""
    wd = 4 if key == "us" else 2
    out, d = [], date.today().replace(day=1)
    while len(out) < n:
        first = d
        off = (wd - first.weekday()) % 7
        third = first + timedelta(days=off + 14)
        if third >= date.today():
            out.append(third.isoformat())
        d = (d + timedelta(days=32)).replace(day=1)
    return out


# ───────── 美股個股 ─────────

def _mid(df):
    bid, ask, last = df.get("bid"), df.get("ask"), df.get("lastPrice")
    m = (bid + ask) / 2
    return m.where((bid > 0) & (ask > 0), last)


def _one(tk, spot, earn_date):
    today = date.today()
    exps = []
    for e in tk.options or []:
        dd = (date.fromisoformat(e) - today).days
        if dd > 0:
            exps.append((e, dd))
    if not exps:
        return None
    sel = [x for x in exps if x[1] <= 45][:4] or exps[:2]
    if earn_date:
        ee = next((x for x in exps if x[0] >= earn_date), None)
        if ee and ee not in sel:
            sel.append(ee)
    calls, puts = [], []
    for e, dd in sel:
        ch = tk.option_chain(e)
        c, p = ch.calls.copy(), ch.puts.copy()
        c["exp"], p["exp"], c["days"], p["days"] = e, e, dd, dd
        calls.append(c)
        puts.append(p)
    C, P = pd.concat(calls), pd.concat(puts)
    for df in (C, P):
        for col in ("openInterest", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    near = [e for e, dd in sel if dd <= 45] or [sel[0][0]]
    Cn, Pn = C[C.exp.isin(near)], P[P.exp.isin(near)]
    it = {"pc_oi": fu._r(Pn.openInterest.sum() / max(Cn.openInterest.sum(), 1)),
          "pc_vol": fu._r(Pn.volume.sum() / max(Cn.volume.sum(), 1)),
          "call_oi": int(Cn.openInterest.sum()), "put_oi": int(Pn.openInterest.sum())}
    # 未平倉最大的履約價：買權＝上方壓力，賣權＝下方支撐
    co = Cn.groupby("strike").openInterest.sum()
    po = Pn.groupby("strike").openInterest.sum()
    if len(co) and co.max() > 0:
        it["call_wall"] = float(co.idxmax())
    if len(po) and po.max() > 0:
        it["put_wall"] = float(po.idxmax())
    # 第一個 7 天以上到期的：價平隱含波動率、預期漲跌幅、最大痛點
    main = next((e for e, dd in sel if dd >= 7), sel[0][0])
    cm, pm = C[C.exp == main], P[P.exp == main]
    if len(cm) and len(pm):
        k = cm.strike.iloc[(cm.strike - spot).abs().argmin()]
        cr, pr = cm[cm.strike == k].iloc[0], pm[pm.strike == k]
        if len(pr):
            pr = pr.iloc[0]
            ivs = [v for v in (cr.get("impliedVolatility"), pr.get("impliedVolatility")) if v and v > 0.02]
            it["iv"] = fu._r(float(np.mean(ivs)) * 100, 1) if ivs else None
            straddle = float(_mid(cm[cm.strike == k]).iloc[0] + _mid(pm[pm.strike == k]).iloc[0])
            it["move_pct"] = fu._r(straddle / spot * 100, 1)
            it["move_exp"] = main
        strikes = np.array(sorted(set(cm.strike) | set(pm.strike)))
        coi = cm.groupby("strike").openInterest.sum()
        poi = pm.groupby("strike").openInterest.sum()
        pain = [(np.maximum(0, s - coi.index.values) * coi.values).sum()
                + (np.maximum(0, poi.index.values - s) * poi.values).sum() for s in strikes]
        if len(pain) and (coi.sum() + poi.sum()) > 0:
            it["max_pain"] = float(strikes[int(np.argmin(pain))])
            it["max_pain_exp"] = main
    if earn_date:
        ee = next((e for e, dd in sel if e >= earn_date), None)
        if ee:
            ce, pe = C[C.exp == ee], P[P.exp == ee]
            if len(ce) and len(pe):
                k = ce.strike.iloc[(ce.strike - spot).abs().argmin()]
                pk = pe[pe.strike == k]
                if len(pk):
                    st = float(_mid(ce[ce.strike == k]).iloc[0] + _mid(pk).iloc[0])
                    it["earn_move_pct"] = fu._r(st / spot * 100, 1)
                    it["earn_date"] = earn_date
    # 異常大量：成交量是未平倉的 3 倍以上、而且至少 1,000 口
    un = []
    for kind, df in (("買權", C), ("賣權", P)):
        m = df[(df.volume >= 1000) & (df.volume > 3 * df.openInterest)]
        for _, r in m.iterrows():
            un.append({"kind": kind, "strike": float(r.strike), "exp": r.exp, "vol": int(r.volume),
                       "oi": int(r.openInterest)})
    if un:
        it["unusual"] = sorted(un, key=lambda x: -x["vol"])[:3]
    it["spot"] = fu._r(spot)
    return it


def us_options(tickers, now_utc, budget=15):
    import yfinance as yf
    path = STATE / "us" / "opt_cache.json"
    cache = fu._load(path, {})
    fund = fu._load(STATE / "us" / "fund_cache.json", {})
    today = date.today().isoformat()
    items = {}
    for t in tickers:
        c = cache.get(t)
        if c and c.get("day") == today:
            items[t] = c["data"]
            continue
        if budget <= 0:
            if c:
                items[t] = c["data"]
            continue
        budget -= 1
        try:
            tk = yf.Ticker(t)
            spot = float(tk.fast_info["last_price"])
            earn = ((fund.get(t) or {}).get("data") or {}).get("earnings", {}).get("date")
            it = _one(tk, spot, earn if earn and earn >= today else None)
        except Exception as e:
            _probe.setdefault("us_errors", []).append(f"{t}: {type(e).__name__}: {e}"[:120])
            if c:
                items[t] = c["data"]
            continue
        if it is None:
            continue
        cache[t] = {"day": today, "data": it}
        items[t] = it
    fu._save(path, cache)
    # 隱含波動率每天記一筆，累積成「現在在過去的位置」
    hp = STATE / "us" / "iv_hist.json"
    h = fu._load(hp, {})
    h[today] = {t: i.get("iv") for t, i in items.items() if i.get("iv")}
    for d in sorted(h)[:-500]:
        del h[d]
    fu._save(hp, h)
    for t, it in items.items():
        series = [h[d][t] for d in sorted(h) if h[d].get(t)]
        it["iv_pct"] = fu._pct_rank(series, it.get("iv")) if len(series) >= 20 else None
        it["iv_days"] = len(series)
    pending = sum(1 for t in tickers if (cache.get(t) or {}).get("day") != today)
    return {"market": "us", "generated_at": _iso(now_utc), "items": items, "pending": pending,
            "settle": settlement_dates("us"),
            "notes": {"pc": "賣權／買權未平倉比：大於 1 代表避險或看空的部位多，太高（例如 1.5 以上）有時反而是反向的見底訊號",
                      "walls": "未平倉最多的買權履約價常是上方壓力，賣權是下方支撐（造市商避險會讓股價被吸在附近）",
                      "move": "價平跨式（買權＋賣權）價格 ÷ 股價＝市場預期到期前的漲跌幅",
                      "unusual": "成交量是未平倉 3 倍以上的合約，可能是新資金進場卡位"}}


# ───────── 台股 ─────────

def tw_options(now_utc):
    out = {"market": "tw", "generated_at": _iso(now_utc), "settle": settlement_dates("tw"), "items": {}}
    hp = STATE / "tw" / "taifex_hist.json"
    h = fu._load(hp, {})
    try:
        rows = fu._fetch("taifex_pcr", f"{TAIFEX}/PutCallRatio")
        recs = []
        for r in rows or []:
            d = fu._roc_to_iso(fu._pick(r, "日期", "Date"))
            pc = fu._num(fu._pick(r, "買賣權未平倉量比率", "PutCallOIRatio", "未平倉量比率"))
            pv = fu._num(fu._pick(r, "買賣權成交量比率", "PutCallVolumeRatio", "成交量比率"))
            if d and pc is not None:
                recs.append((d, pc, pv))
        for d, pc, pv in recs:
            h.setdefault(d, {})["pcr"] = pc
            h[d]["pcr_vol"] = pv
    except Exception:
        _probe["taifex_pcr_parse"] = traceback.format_exc()[-300:]
    try:
        rows = fu._fetch("taifex_inst_fut", f"{TAIFEX}/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
        for r in rows or []:
            name = str(fu._pick(r, "商品名稱", "ContractName", "Contract") or "")
            who = str(fu._pick(r, "身份別", "身分別", "Item", "Identity") or "")
            if "臺股期貨" not in name and "台股期貨" not in name and name.strip() != "TX":
                continue
            if "外資" not in who and "Foreign" not in who:
                continue
            d = fu._roc_to_iso(fu._pick(r, "日期", "Date"))
            net = fu._num(fu._pick(r, "多空未平倉口數淨額", "未平倉口數淨額", "OpenInterestNet", "NetOpenInterest"))
            if d and net is not None:
                h.setdefault(d, {})["fi_net"] = net
    except Exception:
        _probe["taifex_inst_parse"] = traceback.format_exc()[-300:]
    for d in sorted(h)[:-400]:
        del h[d]
    fu._save(hp, h)
    days = sorted(h)
    for k, label in (("pcr", "台指 P/C 比"), ("fi_net", "外資台指期淨單")):
        s = [(d, h[d][k]) for d in days if h[d].get(k) is not None]
        if not s:
            continue
        last = s[-1][1]
        prev = s[-2][1] if len(s) > 1 else None
        out["items"][k] = {"label": label, "value": last, "chg": None if prev is None else fu._r(last - prev, 2),
                           "date": s[-1][0], "spark": [v for _, v in s[-60:]]}
    out["notes"] = {"pcr": "P/C 比＝賣權未平倉 ÷ 買權未平倉（%）。台股習慣上 P/C 比高代表法人賣出很多賣權、看撐，偏多",
                    "fi_net": "外資在台指期的多空未平倉淨口數，正＝淨多單，負＝淨空單；大幅轉向常領先大盤"}
    return out


# ───────── 主程式 ─────────

def run(key, tickers, now_utc, in_session):
    global _probe
    _probe = fu._load(PROBE, {})
    fu._probe = _probe  # 讓 fu._fetch 記到這個檔
    path = DATA_DIR / "options" / f"{key}.json"
    old = fu._load(path, {})
    age_h = 999
    if old.get("generated_at"):
        age_h = (now_utc - datetime.fromisoformat(old["generated_at"].replace("Z", "+00:00"))).total_seconds() / 3600
    try:
        if key == "us":
            # 盤中的選擇權報價會一直變，收盤後一天算一次；分批，還有沒更新的就繼續
            if not in_session and (age_h >= 12 or old.get("pending")):
                fu._save(path, us_options(tickers, now_utc))
        elif age_h >= 4:
            fu._save(path, tw_options(now_utc))
    except Exception:
        _probe[f"{key}_error"] = traceback.format_exc()[-600:]
    _probe["_updated"] = _iso(now_utc)
    try:
        fu._save(PROBE, _probe, compact=False)
    except OSError:
        pass
