"""總經風險燈（0～100，越高越危險）：殖利率、信用利差、AI 借債族群、VIX、油價。

全部用 Yahoo 日線算，同一套函式給網頁儀表板和 5 年回測用。
各項 0～20 分：
- 10 年債殖利率：5% 以上 +10；20 天升 0.25 個百分點以上 +10
- 信用利差：高收益債 HYG ÷ 公債 IEF 跌破 50 日線 +10；投資級債 LQD ÷ IEF 跌破 50 日線 +10
- AI 借債族群（CRWV、ORCL、NBIS、IREN、APLD、CORZ 等權重）÷ SPY：跌破 50 日線 +10；20 天落後 10% 以上 +10
- VIX：20 以上 +10、25 以上 +20
- WTI 原油：20 天漲 8% 以上 +10、15% 以上 +20
50 分以上紅燈、30 分以上黃燈。"""
import numpy as np
import pandas as pd

BASKET = ["CRWV", "ORCL", "NBIS", "IREN", "APLD", "CORZ"]
SYMS = ["^TNX", "HYG", "IEF", "LQD", "^VIX", "CL=F", "SPY"] + BASKET
RED, YELLOW = 50, 30


def _close(data, s):
    df = data.get(s)
    if df is None or len(df) == 0:
        return None
    c = (df["Close"] if "Close" in df else df).dropna()
    c.index = pd.to_datetime(c.index)
    if c.index.tz is not None:
        c.index = c.index.tz_localize(None)
    return c[~c.index.duplicated()]


def series(data):
    """每天的分數表（欄：各項分數、total、light）。data＝{代號: 日線 DataFrame}。"""
    cl = {s: _close(data, s) for s in SYMS}
    spy = cl.get("SPY")
    if spy is None or len(spy) < 60:
        return None
    idx = spy.index
    get = lambda s: cl[s].reindex(idx).ffill() if cl.get(s) is not None else None
    out = pd.DataFrame(index=idx)
    tnx = get("^TNX")
    if tnx is not None:
        tnx = tnx.where(tnx < 20, tnx / 10)  # ^TNX 有時候是 x10
        out["rate"] = (tnx >= 5).astype(int) * 10 + (tnx - tnx.shift(20) >= 0.25).astype(int) * 10
        out["tnx"] = tnx
        out["tnx_chg20"] = tnx - tnx.shift(20)
    ief = get("IEF")
    credit = pd.Series(0, idx)
    for s, name in (("HYG", "hyg"), ("LQD", "lqd")):
        x = get(s)
        if x is not None and ief is not None:
            r = x / ief
            below = r < r.rolling(50).mean()
            out[name + "_below"] = below
            credit = credit + below.astype(int) * 10
    out["credit"] = credit
    rets = pd.concat([get(s).pct_change(fill_method=None) for s in BASKET if get(s) is not None], axis=1)
    if rets.shape[1]:
        bk = (1 + rets.mean(axis=1, skipna=True).fillna(0)).cumprod()
        rel = bk / spy
        below = rel < rel.rolling(50).mean()
        lag = rel / rel.shift(20) - 1
        out["ai_below"] = below
        out["ai_rel20"] = lag
        out["ai"] = below.astype(int) * 10 + (lag <= -0.10).astype(int) * 10
        out["ai_n"] = rets.notna().sum(axis=1)
    else:
        out["ai"] = 0
    vix = get("^VIX")
    if vix is not None:
        out["vix"] = vix
        out["vix_pts"] = np.where(vix >= 25, 20, np.where(vix >= 20, 10, 0))
    oil = get("CL=F")
    if oil is not None:
        o20 = oil / oil.shift(20) - 1
        out["oil_chg20"] = o20
        out["oil_pts"] = np.where(o20 >= 0.15, 20, np.where(o20 >= 0.08, 10, 0))
    pts = [c for c in ("rate", "credit", "ai", "vix_pts", "oil_pts") if c in out]
    out["total"] = out[pts].sum(axis=1)
    out["light"] = np.where(out["total"] >= RED, "red", np.where(out["total"] >= YELLOW, "yellow", "green"))
    return out.iloc[50:]


def latest(data):
    """儀表板用：今天的分數和每一項的狀態。"""
    s = series(data)
    if s is None or not len(s):
        return None
    r = s.iloc[-1]
    f = lambda v, n=2: None if v is None or pd.isna(v) else round(float(v), n)
    inputs = [
        {"k": "rate", "label": "美 10 年債殖利率", "pts": int(r.get("rate", 0)),
         "text": f"{f(r.get('tnx'))}%，20 天 {f(r.get('tnx_chg20')):+.2f}" if pd.notna(r.get("tnx")) else "沒資料"},
        {"k": "credit", "label": "信用利差（HYG、LQD 對公債）", "pts": int(r.get("credit", 0)),
         "text": "、".join(f"{n} {'跌破' if r.get(k) else '站上'} 50 日線" for n, k in (("高收益債", "hyg_below"), ("投資級債", "lqd_below"))
                          if k in r and pd.notna(r.get(k)))},
        {"k": "ai", "label": "AI 借債族群 vs 大盤", "pts": int(r.get("ai", 0)),
         "text": (f"{'跌破' if r.get('ai_below') else '站上'} 50 日線，20 天相對 {f(r.get('ai_rel20', 0) * 100, 1):+.1f}%"
                  if pd.notna(r.get("ai_rel20")) else "沒資料")},
        {"k": "vix", "label": "VIX 恐慌指數", "pts": int(r.get("vix_pts", 0)), "text": f"{f(r.get('vix'))}"},
        {"k": "oil", "label": "WTI 原油 20 天漲跌", "pts": int(r.get("oil_pts", 0)),
         "text": f"{f(r.get('oil_chg20', 0) * 100, 1):+.1f}%" if pd.notna(r.get("oil_chg20")) else "沒資料"},
    ]
    t = s["total"]
    return {"sym": "MACRO", "label": "總經風險燈", "kind": "macro", "value": int(r["total"]), "light": r["light"],
            "chg": int(t.iloc[-1] - t.iloc[-2]) if len(t) > 1 else None, "inputs": inputs,
            "spark": [int(x) for x in t.iloc[-60:]],
            "hist": {"d": {"t": [x.date().isoformat() for x in t.index[-252:]], "v": [int(x) for x in t.iloc[-252:]]},
                     "w": {"t": [x.date().isoformat() for x in t.resample("W-FRI").last().dropna().index[-262:]],
                           "v": [int(x) for x in t.resample("W-FRI").last().dropna().iloc[-262:]]}},
            "basket": BASKET,
            "note": "0～100，越高越危險；50 以上紅燈、30 以上黃燈。殖利率、信用利差、AI 借債族群、VIX、油價各 0～20 分。"
                    "只顯示、還沒放進評分"}


def gates(data):
    """回測用：{名稱: 每天 True＝當天收盤後降到半倉}。"""
    s = series(data)
    if s is None:
        return {}
    g = {"16_macro": s["total"] >= RED}
    if "hyg_below" in s:
        g["16_credit"] = s["hyg_below"].astype(bool)
    return g
