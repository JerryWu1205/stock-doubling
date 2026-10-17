"""技術指標與 0-100 分項評分。

每個分項都回傳 0-100，越高越好（risk 例外：越高代表越危險）。
輸入是單一股票的日線 DataFrame，欄位 Open/High/Low/Close/Volume，日期由舊到新。
"""

import numpy as np
import pandas as pd


def clamp(x, lo=0.0, hi=100.0):
    if x is None or not np.isfinite(x):
        return 50.0
    return float(min(hi, max(lo, x)))


def scale(x, lo, hi):
    """把 x 線性映射到 0-100：x<=lo 得 0，x>=hi 得 100。"""
    if x is None or not np.isfinite(x):
        return 50.0
    return clamp((x - lo) / (hi - lo) * 100.0)


def sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def atr(df, n=14):
    """平均真實波幅（ATR）：一天正常會晃多少錢，用來設停損距離。"""
    prev = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev).abs(), (df["Low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def exit_levels(raw, n_hi=22, k=3.0):
    """退場參考價（用未還原價，才能和報價直接比）：
    trail＝吊燈停損：近 22 天最高收盤 − 3 倍 ATR；sma50＝50 日線；atr＝14 日 ATR。"""
    a = atr(raw).iloc[-1]
    c = raw["Close"]
    hi = c.iloc[-n_hi:].max()
    return {"atr": float(a) if np.isfinite(a) else None,
            "trail": float(hi - k * a) if np.isfinite(a) else None,
            "hi22": float(hi),
            "sma50": float(sma(c, 50).iloc[-1]) if len(c) >= 50 else None}


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def mfi(df, n=14):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    flow = tp * df["Volume"]
    up = flow.where(tp > tp.shift(1), 0.0).rolling(n).sum()
    down = flow.where(tp < tp.shift(1), 0.0).rolling(n).sum()
    ratio = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + ratio)).fillna(100)


def cmf(df, n=20):
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    mfm = ((df["Close"] - df["Low"]) - (df["High"] - df["Close"])) / rng
    mfv = mfm.fillna(0) * df["Volume"]
    return mfv.rolling(n).sum() / df["Volume"].rolling(n).sum().replace(0, np.nan)


def ret(close, n):
    if len(close) <= n:
        return np.nan
    return float(close.iloc[-1] / close.iloc[-1 - n] - 1)


def rs_raw(close, bench_close):
    """相對強弱原始值：個股報酬減大盤報酬，近期權重較重。之後在全體中排百分位。"""
    parts = []
    for n, w in ((21, 0.3), (63, 0.4), (126, 0.3)):
        r, b = ret(close, n), ret(bench_close, n)
        if np.isfinite(r) and np.isfinite(b):
            parts.append((r - b, w))
    if not parts:
        return np.nan
    return sum(v * w for v, w in parts) / sum(w for _, w in parts)


def money_flow_score(df):
    """資金流：MFI 50%、CMF 25%、20 日上漲日成交量占比 25%。"""
    m = float(mfi(df).iloc[-1])
    c = float(cmf(df).iloc[-1])
    last = df.iloc[-20:]
    up_days = last["Close"] > last["Close"].shift(1)
    tot = last["Volume"].sum()
    up_ratio = float(last["Volume"][up_days].sum() / tot) if tot > 0 else 0.5
    return (0.5 * scale(m, 20, 80) + 0.25 * scale(c, -0.25, 0.25)
            + 0.25 * scale(up_ratio, 0.3, 0.7))


def trend_score(df):
    """趨勢：五個條件各 20 分。"""
    c = df["Close"]
    s20, s50, s200 = sma(c, 20), sma(c, 50), sma(c, 200)
    last = c.iloc[-1]
    checks = [
        last > s20.iloc[-1],
        last > s50.iloc[-1],
        last > s200.iloc[-1] if np.isfinite(s200.iloc[-1]) else last > s50.iloc[-1],
        s50.iloc[-1] > s200.iloc[-1] if np.isfinite(s200.iloc[-1]) else s20.iloc[-1] > s50.iloc[-1],
        s20.iloc[-1] > s20.iloc[-6],
    ]
    return 20.0 * sum(bool(x) for x in checks)


def rebound_score(df):
    """反彈：多頭中的拉回是否正在止跌回升。

    - 拉回深度（距 63 日高點）落在 5%-20% 最理想 40 分
    - RSI 近 5 日曾低於 40 且現在回升 30 分
    - 今天收紅且收在當日區間上半部 15 分
    - 股價靠近 50 日均線（±4%）且 200 日均線向上 15 分
    """
    c = df["Close"]
    hi63 = c.iloc[-63:].max()
    dd = 1 - c.iloc[-1] / hi63
    if dd < 0.05:
        depth = dd / 0.05 * 20
    elif dd <= 0.20:
        depth = 40
    else:
        depth = max(0.0, 40 - (dd - 0.20) / 0.15 * 40)

    r = rsi(c)
    rsi_turn = 30.0 if (r.iloc[-6:-1].min() < 40 and r.iloc[-1] > r.iloc[-2]) else 0.0

    bar = df.iloc[-1]
    rng = bar["High"] - bar["Low"]
    upper = rng > 0 and (bar["Close"] - bar["Low"]) / rng >= 0.5
    candle = 15.0 if (c.iloc[-1] > c.iloc[-2] and upper) else 0.0

    s50, s200 = sma(c, 50), sma(c, 200)
    near = abs(c.iloc[-1] / s50.iloc[-1] - 1) <= 0.04 if np.isfinite(s50.iloc[-1]) else False
    rising = (s200.iloc[-1] > s200.iloc[-21]) if np.isfinite(s200.iloc[-21]) else True
    support = 15.0 if (near and rising) else 0.0

    return clamp(depth + rsi_turn + candle + support)


def risk_score(df):
    """風險（越高越危險）：20 日年化波動率與 63 日最大回撤各半。"""
    c = df["Close"]
    vol = float(c.pct_change().iloc[-20:].std() * np.sqrt(252))
    window = c.iloc[-63:]
    mdd = float((1 - window / window.cummax()).max())
    return 0.5 * scale(vol, 0.20, 1.20) + 0.5 * scale(mdd, 0.05, 0.50)


def percentile_rank(values):
    """dict ticker->value 轉成 0-100 百分位。缺值給 50。"""
    s = pd.Series(values, dtype=float)
    ranked = s.rank(pct=True) * 100
    return {k: (float(ranked[k]) if np.isfinite(s[k]) else 50.0) for k in s.index}
