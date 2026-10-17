"""走勢圖資料：均線、自動趨勢線、支撐壓力、量價指標，以及一句話提示。

這些只給網頁畫圖和提示用，不影響評分。
輸入是未還原的日線（Open/High/Low/Close/Volume），和看盤軟體上看到的價格一致。
"""

import numpy as np
import pandas as pd

import indicators as ind

CHART_BARS = 252     # 圖上保留一年
PIVOT_WIN = 5        # 轉折點：前後各 5 根 K 都比它低（高）
LEVEL_BAND = 0.02    # 支撐壓力：相差 2% 以內的轉折點算同一區


def _r(x, nd=None):
    if x is None or not np.isfinite(x):
        return None
    if nd is None:
        nd = 2 if abs(x) >= 1 else 4
    return round(float(x), nd)


def _arr(s, nd=None):
    return [_r(v, nd) for v in s.to_numpy(dtype=float)]


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def obv(df):
    sign = np.sign(df["Close"].diff().fillna(0))
    return (sign * df["Volume"]).cumsum()


def macd(close):
    line = ema(close, 12) - ema(close, 26)
    sig = ema(line, 9)
    return line, sig, line - sig


def pivots(df, win=PIVOT_WIN):
    """回傳 (高點 [(i, price)], 低點 [(i, price)])。最後 win 根 K 還無法確認，不算。"""
    hi, lo = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    highs, lows = [], []
    for i in range(win, len(df) - win):
        h, l = hi[i - win:i + win + 1], lo[i - win:i + win + 1]
        if hi[i] == h.max() and (h == hi[i]).sum() == 1:
            highs.append((i, hi[i]))
        if lo[i] == l.min() and (l == lo[i]).sum() == 1:
            lows.append((i, lo[i]))
    return highs, lows


def trendline(points, close, kind):
    """kind='up'：連最近一個低點和更早、更低的低點；kind='down'：連高點，越早越高。

    從最新的轉折點往回找，挑「兩點之間收盤沒有明顯穿越」且最長的一條。
    回傳 {i1, v1, i2, v2, slope} 或 None。
    """
    if len(points) < 2:
        return None
    n = len(close)
    i2, v2 = points[-1]
    best = None
    for i1, v1 in reversed(points[:-1]):
        if i2 - i1 < 10:
            continue
        if kind == "up" and v1 >= v2 or kind == "down" and v1 <= v2:
            continue
        slope = (v2 - v1) / (i2 - i1)
        seg = np.arange(i1, i2 + 1)
        line = v1 + slope * (seg - i1)
        c = close[i1:i2 + 1]
        bad = (c < line * 0.99).sum() if kind == "up" else (c > line * 1.01).sum()
        if bad > 1:
            continue
        end = v1 + slope * (n - 1 - i1)
        # 延伸到今天要合理：不能跑到負數，也不能離現價太遠
        if end <= 0 or abs(end / close[-1] - 1) > 0.35:
            continue
        best = {"i1": i1, "v1": v1, "i2": i2, "v2": v2, "slope": slope}
    return best


def line_at(tl, i):
    return tl["v1"] + tl["slope"] * (i - tl["i1"])


def levels(highs, lows, last_close):
    """把轉折點價格分群，回傳現價下方、上方最近各 2 個價位帶。"""
    prices = sorted(p for _, p in highs + lows)
    groups = []
    for p in prices:
        if groups and p <= groups[-1]["lo"] * (1 + LEVEL_BAND):
            g = groups[-1]
            g["members"].append(p)
            g["hi"] = p
        else:
            groups.append({"lo": p, "hi": p, "members": [p]})
    out = []
    for g in groups:
        price = float(np.mean(g["members"]))
        out.append({"price": price, "lo": g["lo"], "hi": g["hi"], "touches": len(g["members"])})
    below = sorted((g for g in out if g["price"] < last_close), key=lambda g: -g["price"])[:2]
    above = sorted((g for g in out if g["price"] >= last_close), key=lambda g: g["price"])[:2]
    return ([dict(g, kind="support") for g in below] + [dict(g, kind="resistance") for g in above],
            out)


def analyze(raw):
    """回傳 (圖表用的 dict, 提示 list)。raw 是未還原日線，至少要 60 根。"""
    df = raw.iloc[-(CHART_BARS + 260):]  # 多留一些讓 200 日線在圖的開頭就有值
    c = df["Close"]
    n = len(df)
    s20, s50, s200 = ind.sma(c, 20), ind.sma(c, 50), ind.sma(c, 200)
    vma = df["Volume"].rolling(20, min_periods=20).mean()
    o = obv(df)
    m_line, m_sig, m_hist = macd(c)
    rsi = ind.rsi(c)
    mfi = ind.mfi(df)
    cmf = ind.cmf(df)

    view = slice(max(0, n - CHART_BARS), n)
    off = view.start
    win = df.iloc[view]
    highs, lows = pivots(win)
    close = win["Close"].to_numpy(float)
    up = trendline(lows, close, "up")
    down = trendline(highs, close, "down")
    lv, all_groups = levels(highs, lows, close[-1])

    hi52 = float(win["High"].iloc[:-1].max()) if len(win) > 1 else float(win["High"].iloc[-1])
    lo52 = float(win["Low"].iloc[:-1].min()) if len(win) > 1 else float(win["Low"].iloc[-1])

    # ── 提示（只當參考，不進評分）──
    alerts = []
    last, prev = close[-1], close[-2] if len(close) > 1 else close[-1]
    k = len(close) - 1
    if up and up["i2"] < k - 1:
        if last < line_at(up, k) * 0.99 and prev >= line_at(up, k - 1) * 0.99:
            alerts.append("跌破上升趨勢線")
    if down and down["i2"] < k - 1:
        if last > line_at(down, k) * 1.01 and prev <= line_at(down, k - 1) * 1.01:
            alerts.append("突破下降趨勢線")
    strong = [g for g in all_groups if g["touches"] >= 2]
    if any(prev <= g["hi"] < last for g in strong):
        alerts.append("突破壓力區")
    if any(prev >= g["lo"] > last for g in strong):
        alerts.append("跌破支撐區")
    if float(win["High"].iloc[-1]) > hi52 and len(win) >= 200:
        alerts.append("創 52 週新高")
    v_last, v_avg = float(df["Volume"].iloc[-1]), float(vma.iloc[-2]) if n > 21 else np.nan
    if np.isfinite(v_avg) and v_avg > 0 and v_last > 2 * v_avg:
        alerts.append("爆量上漲" if last > prev else "爆量下跌" if last < prev else "爆量")
    if n > 21 and last >= c.iloc[-21:-1].max() and o.iloc[-1] < o.iloc[-21:-1].max():
        alerts.append("量價背離")

    dates = [d.date().isoformat() for d in win.index]

    def tl_out(tl, kind):
        if not tl:
            return None
        now = line_at(tl, k)
        broken = close[-1] < now * 0.99 if kind == "up" else close[-1] > now * 1.01
        return {"kind": kind, "broken": bool(broken),
                "from": {"t": dates[tl["i1"]], "v": _r(tl["v1"])},
                "to": {"t": dates[-1], "v": _r(now)},
                "pivot2": {"t": dates[tl["i2"]], "v": _r(tl["v2"])}}

    sl = lambda s: _arr(s.iloc[view])
    chart = {
        "t": dates,
        "o": sl(df["Open"]), "h": sl(df["High"]), "l": sl(df["Low"]), "c": sl(c),
        "v": [int(x) if np.isfinite(x) else 0 for x in win["Volume"].to_numpy(float)],
        "sma20": sl(s20), "sma50": sl(s50), "sma200": sl(s200),
        "vma20": [int(x) if np.isfinite(x) else None for x in vma.iloc[view].to_numpy(float)],
        "mfi": _arr(mfi.iloc[view], 1), "rsi": _arr(rsi.iloc[view], 1),
        "cmf": _arr(cmf.iloc[view], 3),
        "obv": [int(x) for x in o.iloc[view].to_numpy(float)],
        "macd": _arr(m_line.iloc[view], 3), "macd_signal": _arr(m_sig.iloc[view], 3),
        "macd_hist": _arr(m_hist.iloc[view], 3),
        "trendlines": [x for x in (tl_out(up, "up"), tl_out(down, "down")) if x],
        "levels": [{"kind": g["kind"], "price": _r(g["price"]), "lo": _r(g["lo"]),
                    "hi": _r(g["hi"]), "touches": g["touches"]} for g in lv],
        "hi52": _r(hi52), "lo52": _r(lo52),
    }
    return chart, alerts


def dividend_dates(raw):
    """還原因子（Adj Close / Close）跳動的日子 = 除權息日。"""
    if "Adj Close" not in raw:
        return []
    f = (raw["Adj Close"] / raw["Close"]).replace([np.inf, -np.inf], np.nan).dropna()
    jump = f.pct_change().abs() > 0.003
    return [d.date().isoformat() for d in f.index[jump.fillna(False).to_numpy()]]


def vwap(df):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    vol = df["Volume"].fillna(0)
    cum = vol.cumsum().replace(0, np.nan)
    return ((tp * vol).cumsum() / cum).fillna(df["Close"])
