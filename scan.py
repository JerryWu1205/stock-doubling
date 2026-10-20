"""股票翻倍計畫：掃描器主程式（美股、台股）。

用法（在這個資料夾裡執行）：
  python scan.py                   自動判斷：每個市場盤中跑「盤中暫定」，收盤後跑「收盤確認」，休市就跳過
  python scan.py --market tw       只跑台股（us / tw / all，預設 all）
  python scan.py --mode intraday   強制盤中暫定
  python scan.py --mode close      強制收盤確認（會記入歷史，用於汰弱留強的連續天數）
  python scan.py --backfill 5      補算過去 5 個交易日的收盤分數（新市場第一次跑時會自動做）
  python scan.py --demo            不連網，用模擬資料跑一次（測試用）

輸出 docs/data/<市場>.json（列表）、docs/data/charts/<市場>/<代號>.json（走勢圖）、
docs/data/intraday/<市場>.json（今日走勢）和 docs/data/markets.json。
只含代號、股價、分數與訊號，不含任何持股金額。
"""

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tomllib
import traceback
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import backtest as bt
import sim
import twrt
import charts
import indicators as ind

HERE = Path(__file__).resolve().parent
DOCS = HERE / "docs"
DATA_DIR = DOCS / "data"
HISTORY_KEEP_DAYS = 60
BT_V = 3  # 回測邏輯改了就加 1，下次收盤（或週末）會重算績效頁
MIN_BARS = 130

# 每個市場的時間規則。open~close 之間跑盤中暫定；close~settle_end 重跑幾次收盤確認，讓 Yahoo 資料定稿。
MARKETS = {
    "us": {"label": "美股", "tz": "America/New_York", "tz_label": "美東", "currency": "USD",
           "open": "09:30", "trade_close": "16:00", "close": "16:00", "settle_end": "18:00",
           "benchmark": "SPY", "quote_delay_minutes": 0, "price_limit_pct": None},
    "tw": {"label": "台股", "tz": "Asia/Taipei", "tz_label": "台北", "currency": "TWD",
           "open": "09:00", "trade_close": "13:30", "close": "14:30", "settle_end": "16:00",
           "benchmark": "0050", "quote_delay_minutes": 20, "price_limit_pct": 10},
}

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
    "rebound": "反彈中",
    "take": "反彈達標",
}

# 兩套名單：強勢（原本的評分）和跌深（買超跌、開始止跌的股票）
BOOKS = {
    "strong": {"label": "強勢", "components": [["rs", "相對強弱"], ["mf", "資金流"], ["trend", "趨勢"],
                                                ["rebound", "反彈"], ["risk", "風險"]]},
    "dip": {"label": "跌深", "components": [["depth", "跌深"], ["oversold", "超賣"], ["reversal", "止跌"],
                                             ["mfturn", "資金回流"], ["risk", "風險"]]},
}


def hm(s):
    h, m = s.split(":")
    return time(int(h), int(m))


# ───────── 設定 ─────────

def load_config():
    """回傳 {市場代號: cfg}。舊格式（沒有 [us] 段落）當成只有美股。"""
    with open(HERE / "config.toml", "rb") as f:
        raw = tomllib.load(f)
    shared = {k: raw[k] for k in ("swap_gap", "swap_days", "weights", "exit") if k in raw}
    if not any(k in raw for k in MARKETS):
        sections = {"us": raw}
    else:
        sections = {k: raw[k] for k in MARKETS if k in raw and raw[k].get("enabled", True)}
    return {key: market_config(key, {**shared, **sec}) for key, sec in sections.items()}


def market_config(key, cfg):
    m = {**MARKETS[key], **{k: v for k, v in cfg.items() if k in MARKETS[key]}}
    norm = (lambda t: str(t).upper()) if key == "us" else (lambda t: str(t).strip().upper())
    themes = {}
    for theme, tickers in cfg.get("candidates", {}).items():
        for t in tickers:
            themes.setdefault(norm(t), theme)
    hold_cfg = cfg.get("holdings", {})
    holdings = [norm(t) for t in hold_cfg.get("tickers", [])]
    for t, theme in cfg.get("holding_themes", {}).items():
        themes[norm(t)] = theme
    pool = list(dict.fromkeys(
        norm(t) for ts in cfg.get("candidates", {}).values() for t in ts
        if norm(t) not in holdings))
    w = cfg.get("weights", {})
    state = HERE / "state" / key
    return {
        **m,
        "key": key,
        "zone": ZoneInfo(m["tz"]),
        "benchmark": norm(cfg.get("benchmark", m["benchmark"])),
        "swap_gap": float(cfg.get("swap_gap", 15)),
        "swap_days": int(cfg.get("swap_days", 3)),
        "weights": {
            "relative_strength": w.get("relative_strength", 30),
            "money_flow": w.get("money_flow", 25),
            "trend": w.get("trend", 20),
            "rebound": w.get("rebound", 15),
            "risk": w.get("risk", 10),
        },
        # 出場規則：網頁、Telegram 持股報告、模擬倉共用（config.toml 的 [exit]）
        "exit_policy": {"stop_pct": float(cfg.get("exit", {}).get("stop_pct", 8))},
        "fixed": holdings,  # 你自己指定的持股，程式不會自動換掉
        "pool": pool,
        "target_count": int(hold_cfg.get("target_count", 16)),
        "max_per_theme": int(hold_cfg.get("max_per_theme", 4)),
        "core": norm(str(hold_cfg["core"])) if hold_cfg.get("core") else None,  # 核心部位（台股 0050），一直持有、不換股
        "core_weight": float(hold_cfg.get("core_weight", 0.5)),
        "holdings": list(holdings),
        "candidates": list(pool),
        "themes": themes,
        "names": {**(US_NAMES if key == "us" else {}), **{norm(k): v for k, v in cfg.get("names", {}).items()}},
        "history_path": state / "history.json",
        "portfolio_path": state / "portfolio.json",
        "suffix_path": state / "suffix.json",
    }


def migrate_state():
    """舊版把美股狀態放在 state/ 底下，搬到 state/us/，換股連續天數不會歸零。"""
    for name in ("history.json", "portfolio.json"):
        old, new = HERE / "state" / name, HERE / "state" / "us" / name
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(old, new)
            print(f"已把 state/{name} 搬到 state/us/{name}")


# ───────── 自動選股組合 ─────────
# 持股 = config 裡你自己寫的代號 + 程式自動挑的代號（存在 state/<市場>/portfolio.json）。
# 名額不足 target_count 時，從候選池挑分數最高的補滿，每個主題最多 max_per_theme 檔。
# 換股確認後，若最弱的是自動挑的那檔，程式會直接換掉；若是你自己指定的，只發訊號。

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


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


def fill_portfolio(cfg, port, results, date, gate=False):
    """gate=True：只挑「可進場」的股票，沒有符合的就先空著（等回檔、等止跌），不硬補。"""
    keep_n = max(cfg["target_count"] - len(cfg["fixed"]), 0)
    if len(port["auto"]) > keep_n:  # 持股檔數調少了：留分數最高的（每個主題最多 max_per_theme 檔），其餘移出
        cnt = {}
        for t in cfg["fixed"]:
            cnt[cfg["themes"].get(t, "")] = cnt.get(cfg["themes"].get(t, ""), 0) + 1
        keep = []
        for t in sorted(port["auto"], key=lambda t: -results.get(t, {}).get("score", -1)):
            th = cfg["themes"].get(t, "")
            if len(keep) < keep_n and cnt.get(th, 0) < cfg["max_per_theme"]:
                keep.append(t)
                cnt[th] = cnt.get(th, 0) + 1
        dropped = [t for t in port["auto"] if t not in keep]
        port["auto"] = keep
        port["changes"].append({"date": date, "action": "trim", "tickers": dropped,
                                "note": f"持股檔數改成 {cfg['target_count']} 檔"})
        apply_portfolio(cfg, port)
    counts = theme_counts(cfg)
    ranked = sorted((t for t in cfg["candidates"] if t in results
                     and (not gate or results[t].get("entry_ok"))),
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


def execute_swap(cfg, port, swaps, results, date, gate=False):
    """收盤確認的換股：只換自動挑的持股，一天最多換一檔。"""
    if any(c.get("action") == "swap" and c.get("date") == date for c in port["changes"]):
        return None  # 收盤後會重跑幾次，同一天已經換過就不再換
    ready = sorted((s for s in swaps.items()
                    if s[1]["confirmed_streak"] >= cfg["swap_days"]
                    and (not gate or results.get(s[0], {}).get("entry_ok"))),
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

def market_session(cfg, now):
    """open = 當地平日 open~close；其他時間 closed。國定假日由資料日期判斷。"""
    if now.weekday() < 5 and hm(cfg["open"]) <= now.time() < hm(cfg["close"]):
        return "open"
    return "closed"


def expected_close_date(cfg, now):
    """最近一個「已經收盤」的平日（不管國定假日，假日由資料判斷）。"""
    d = pd.Timestamp(now.date())
    if now.weekday() >= 5 or now.time() < hm(cfg["close"]):
        d -= pd.offsets.BDay(1)
    return d.date().isoformat()


def minutes_until_next_open(cfg, now_utc):
    """收盤確認後，到下一個平日開盤後 45 分鐘（第一筆盤中資料應該出現）為止都不算過期。"""
    t = now_utc.astimezone(cfg["zone"])
    d = t.date() if t.time() < hm(cfg["open"]) else t.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    nxt = datetime.combine(d, hm(cfg["open"]), tzinfo=cfg["zone"]) + timedelta(minutes=45)
    return int((nxt - t).total_seconds() // 60)


def date_lt(d, iso):
    return d.isoformat() < iso


def config_newer(key):
    """config.toml 比網頁資料新（剛加了候選股）就要重跑一次。"""
    try:
        return (HERE / "config.toml").stat().st_mtime > (DATA_DIR / f"{key}.json").stat().st_mtime
    except OSError:
        return False


def nothing_to_do(cfg, now, hist):
    """自動模式下，休市時段且最近一次收盤已確認，就不必再抓資料。

    排程可以全天每 15 分鐘呼叫，程式自己決定要不要跑；
    收盤後到 settle_end 會重跑幾次，讓 Yahoo 的收盤資料定稿。
    """
    if market_session(cfg, now) == "open":
        return False
    if now.weekday() < 5 and hm(cfg["close"]) <= now.time() < hm(cfg["settle_end"]):
        return False
    exp = expected_close_date(cfg, now)
    return exp in hist["confirmed"] or hist.get("holiday_checked") == exp


# ───────── 資料 ─────────
# 用 auto_adjust=False 一次抓到未還原價（給股價和走勢圖）和還原收盤（給評分，除息不會打亂均線）。

FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]


def _split_download(raw, symbols):
    out = {}
    if raw is None or raw.empty:
        return out
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        for s in symbols:
            if s in level0:
                out[s] = raw[s]
    elif len(symbols) == 1:
        out[symbols[0]] = raw
    clean = {}
    for s, df in out.items():
        cols = [c for c in FIELDS if c in df.columns]
        df = df[cols].dropna(subset=["Close"])
        if len(df):
            df = df.copy()
            if "Adj Close" not in df:
                df["Adj Close"] = df["Close"]
            df["Volume"] = df["Volume"].fillna(0)
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            clean[s] = df[~df.index.duplicated(keep="last")]
    return clean


def adjusted(raw):
    """未還原日線 → 還原日線（評分用）。"""
    f = (raw["Adj Close"] / raw["Close"]).replace([np.inf, -np.inf], np.nan).fillna(1.0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
    for c in ("Open", "High", "Low", "Close"):
        df[c] = raw[c] * f
    return df


def yf_download(symbols, **kw):
    import yfinance as yf
    base = dict(group_by="ticker", progress=False, threads=True, auto_adjust=False)
    return _split_download(yf.download(symbols, **{**base, **kw}), symbols)


def fetch_daily(symbols):
    kw = dict(period="2y", interval="1d")
    data = yf_download(symbols, **kw)
    for s in [s for s in symbols if s not in data]:  # 失敗的逐檔再試一次
        try:
            data.update(yf_download([s], **kw))
        except Exception:
            pass
    return data


def resolve_tw(codes, cfg):
    """台股代號自動判斷上市（.TW）或上櫃（.TWO），結果存起來，下次不用再猜。"""
    cache = read_json(cfg["suffix_path"], {})
    todo = [c for c in codes if c not in cache and not any(c.endswith(x) for x in (".TW", ".TWO"))]
    for suffix in (".TW", ".TWO"):
        if not todo:
            break
        got = yf_download([c + suffix for c in todo], period="5d", interval="1d")
        for c in list(todo):
            if c + suffix in got:
                cache[c] = suffix
                todo.remove(c)
    if todo:
        print(f"台股找不到上市或上櫃資料：{', '.join(todo)}")
    write_json_atomic(cfg["suffix_path"], dict(sorted(cache.items())))
    return {c: (c if c.endswith((".TW", ".TWO")) else c + cache[c]) for c in codes if c in cache
            or c.endswith((".TW", ".TWO"))}


def symbol_map(cfg, tickers, demo):
    if cfg["key"] == "us" or demo:
        return {t: t for t in tickers}
    return resolve_tw(tickers, cfg)


def _intraday_raw(symmap, cfg, period="1d", prepost=False):
    """yf_download 會把時間 normalize 掉，盤中要保留時間，所以這裡另外處理。"""
    import yfinance as yf
    syms = list(symmap.values())
    raw = yf.download(syms, period=period, interval="5m", prepost=prepost, group_by="ticker",
                      progress=False, threads=True, auto_adjust=False)
    out = {}
    if raw is None or raw.empty:
        return out
    for t, s in symmap.items():
        if isinstance(raw.columns, pd.MultiIndex):
            if s not in set(raw.columns.get_level_values(0)):
                continue
            df = raw[s]
        elif len(syms) == 1:
            df = raw
        else:
            continue
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
        if len(df):
            idx = pd.to_datetime(df.index)
            idx = idx.tz_localize("UTC") if idx.tz is None else idx
            df = df.copy()
            df.index = idx.tz_convert(cfg["zone"])
            out[t] = df
    return out


# 美股中文名（config 的 [us.names] 可以覆蓋）
US_NAMES = {
    "SPY": "S&P 500 ETF",
    "NVDA": "輝達",
    "AMD": "超微",
    "AVGO": "博通",
    "TSM": "台積電ADR",
    "MU": "美光",
    "ASML": "艾司摩爾",
    "AMAT": "應用材料",
    "LRCX": "科林研發",
    "KLAC": "科磊",
    "MRVL": "邁威爾",
    "ARM": "安謀",
    "QCOM": "高通",
    "INTC": "英特爾",
    "TXN": "德州儀器",
    "ADI": "亞德諾",
    "NXPI": "恩智浦",
    "ON": "安森美",
    "MPWR": "芯源系統",
    "TER": "泰瑞達",
    "ALAB": "Astera Labs",
    "SMCI": "美超微",
    "SOXL": "三倍做多半導體ETF",
    "COHR": "Coherent",
    "LITE": "Lumentum",
    "CIEN": "Ciena",
    "AAOI": "應用光電",
    "FN": "Fabrinet",
    "CRDO": "Credo",
    "ANET": "Arista",
    "GLW": "康寧",
    "CLS": "Celestica",
    "MTSI": "MACOM",
    "VST": "Vistra",
    "CEG": "Constellation能源",
    "GEV": "GE Vernova",
    "OKLO": "Oklo",
    "SMR": "NuScale",
    "CCJ": "Cameco",
    "NRG": "NRG能源",
    "TLN": "Talen能源",
    "BWXT": "BWX科技",
    "LEU": "Centrus能源",
    "NNE": "Nano Nuclear",
    "ETN": "伊頓",
    "VRT": "維諦",
    "PWR": "Quanta Services",
    "CRWV": "CoreWeave",
    "NBIS": "Nebius",
    "IREN": "IREN",
    "CIFR": "Cipher Mining",
    "APLD": "Applied Digital",
    "WULF": "TeraWulf",
    "CORZ": "Core Scientific",
    "ORCL": "甲骨文",
    "PLTR": "Palantir",
    "MSFT": "微軟",
    "CRM": "Salesforce",
    "NOW": "ServiceNow",
    "SNOW": "Snowflake",
    "DDOG": "Datadog",
    "CRWD": "CrowdStrike",
    "NET": "Cloudflare",
    "PANW": "Palo Alto",
    "ZS": "Zscaler",
    "MDB": "MongoDB",
    "APP": "AppLovin",
    "SHOP": "Shopify",
    "ADBE": "Adobe",
    "XOM": "埃克森美孚",
    "CVX": "雪佛龍",
    "OXY": "西方石油",
    "COP": "康菲石油",
    "SLB": "斯倫貝謝",
    "EOG": "EOG資源",
    "DVN": "Devon能源",
    "HAL": "哈里伯頓",
}


# 美股盤前（美東 4:00–9:30）、盤後（16:00–20:00）：只顯示價格當參考，不進評分
EXT_HOURS = {"pre": ("04:00", "09:30"), "post": ("16:00", "20:00")}


def ext_session(cfg, now):
    if cfg["key"] != "us" or now.weekday() >= 5:
        return None
    for k, (a, b) in EXT_HOURS.items():
        if hm(a) <= now.time() < hm(b):
            return k
    return None


def write_ext(cfg, tickers, now, now_utc, demo=False):
    """抓含盤前盤後的 5 分鐘線，算出每檔最新的盤前/盤後價，和最近一次正常盤收盤比。"""
    sess = ext_session(cfg, now)
    if sess is None:
        return None
    symmap = {t: t for t in tickers}
    frames = demo_ext(tickers, cfg, now, sess) if demo else _intraday_raw(symmap, cfg, period="5d", prepost=True)
    items = {}
    for t, df in frames.items():
        tod = df.index.time
        reg = df[(tod >= hm(cfg["open"])) & (tod < hm(cfg["close"]))]
        if reg.empty:
            continue
        ext = df[df.index > reg.index[-1]]
        ext = ext[ext.index.date == now.date()]
        if ext.empty:
            continue
        base = float(reg["Close"].iloc[-1])
        px = float(ext["Close"].iloc[-1])
        items[t] = {"p": charts._r(px), "chg": round((px / base - 1) * 100, 2),
                    "t": ext.index[-1].strftime("%H:%M")}
    out = {"market": cfg["key"], "session": sess, "label": "盤前" if sess == "pre" else "盤後",
           "date": now.date().isoformat(),
           "generated_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
           "demo": demo, "items": items}
    write_json_atomic(DATA_DIR / "ext" / f"{cfg['key']}.json", out)
    return out


def demo_ext(tickers, cfg, now, sess):
    rng = np.random.default_rng(int(now.strftime("%j")))
    out = {}
    day = pd.Timestamp(now.date()).tz_localize(cfg["zone"])
    for t in tickers:
        idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=30), now.replace(second=0, microsecond=0)
                            if sess == "post" else day + pd.Timedelta(hours=16), freq="5min", inclusive="left")
        if sess == "pre":  # 盤前：昨天的正常盤＋今天早上
            prev = pd.date_range(day - pd.Timedelta(days=1) + pd.Timedelta(hours=9, minutes=30),
                                 day - pd.Timedelta(days=1) + pd.Timedelta(hours=16), freq="5min", inclusive="left")
            idx = prev.append(pd.date_range(day + pd.Timedelta(hours=4), now.replace(second=0, microsecond=0),
                                            freq="5min", inclusive="left"))
        if len(idx) == 0:
            continue
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, len(idx))))
        out[t] = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1000}, index=idx)
    return out


def fetch_demo(tickers, end=None, key="us"):
    """模擬資料：每檔固定亂數種子，結果可重現。"""
    end = pd.Timestamp(end or datetime.now(timezone.utc).date())
    idx = pd.bdate_range(end=end, periods=520)
    data = {}
    for t in tickers:
        rng = np.random.default_rng(int(hashlib.md5((key + t).encode()).hexdigest()[:8], 16))
        drift = rng.normal(0.0006, 0.0012)
        vol = rng.uniform(0.015, 0.05)
        if key == "tw":
            vol = min(vol, 0.03)
        r = rng.normal(drift, vol, len(idx))
        if key == "tw":
            r = np.clip(r, -0.095, 0.095)
            r[-1] = 0.0999 if rng.random() < 0.08 else r[-1]  # 偶爾漲停，測試用
        start = rng.uniform(20, 300) if key == "us" else rng.uniform(20, 1200)
        close = start * np.exp(np.cumsum(r))
        spread = np.abs(rng.normal(0, vol, len(idx))) * close
        opn = close * (1 - r / 2)
        v = rng.integers(1_000_000, 20_000_000, len(idx)).astype(float)
        v[rng.random(len(idx)) < 0.04] *= 3
        df = pd.DataFrame({
            "Open": opn,
            "High": np.maximum(opn, close) + spread / 2,
            "Low": np.minimum(opn, close) - spread / 2,
            "Close": close,
            "Volume": v,
        }, index=idx)
        # 兩次除息，測試還原價
        factor = np.ones(len(idx))
        for k in rng.choice(np.arange(300, len(idx) - 5), 2, replace=False):
            factor[:k] *= 0.985 if key == "us" else 0.96
        df["Adj Close"] = df["Close"] * factor
        if key == "tw" and r[-1] >= 0.0999:
            df.iloc[-1, df.columns.get_loc("High")] = df["Close"].iloc[-1]
        data[t] = df
    return data


def demo_intraday(daily, cfg, now):
    out = {}
    day = now.date()
    o, c = hm(cfg["open"]), hm(cfg["trade_close"])
    start = datetime.combine(day, o, tzinfo=cfg["zone"])
    end = min(now, datetime.combine(day, c, tzinfo=cfg["zone"]))
    if end <= start:
        end = datetime.combine(day, c, tzinfo=cfg["zone"])
    idx = pd.date_range(start, end, freq="5min", inclusive="left")
    for t, df in daily.items():
        rng = np.random.default_rng(int(hashlib.md5(("i" + t).encode()).hexdigest()[:8], 16))
        prev, last = float(df["Close"].iloc[-2]), float(df["Close"].iloc[-1])
        path = np.linspace(prev, last, len(idx)) * np.exp(np.cumsum(rng.normal(0, 0.002, len(idx))))
        path[-1] = last
        out[t] = pd.DataFrame({"Open": path, "High": path * 1.001, "Low": path * 0.999,
                               "Close": path, "Volume": rng.integers(1e4, 3e5, len(idx)).astype(float)},
                              index=idx)
    return out


# ───────── 台股漲跌停 ─────────

def tw_tick(price):
    for lim, tick in ((10, 0.01), (50, 0.05), (100, 0.1), (500, 0.5), (1000, 1.0)):
        if price < lim:
            return tick
    return 5.0


def limit_state(cfg, ticker, raw):
    """收盤價等於漲停 / 跌停價就標 up / down。ETF（00 開頭）跳動單位不同，不判斷。"""
    pct = cfg.get("price_limit_pct")
    if not pct or len(raw) < 2 or ticker.startswith("00"):
        return None
    prev, last = float(raw["Close"].iloc[-2]), float(raw["Close"].iloc[-1])
    up_px = prev * (1 + pct / 100)
    dn_px = prev * (1 - pct / 100)
    tu, td = tw_tick(up_px), tw_tick(dn_px)
    up_px = math.floor(up_px / tu + 1e-9) * tu
    dn_px = math.ceil(dn_px / td - 1e-9) * td
    if last >= up_px - tu / 2:
        return "up"
    if last <= dn_px + td / 2:
        return "down"
    return None


# ───────── 評分 ─────────

def score_universe(raw_data, cfg, asof=None):
    """回傳 ({ticker: {...}}, errors)，所有分項與總分。asof 可截斷到某一天（補算用）。"""
    bench = raw_data.get(cfg["benchmark"])
    if bench is None:
        raise RuntimeError(f"抓不到大盤基準 {cfg['benchmark']} 的資料")
    bench = adjusted(bench)
    if asof is not None:
        bench = bench[bench.index <= asof]
    universe = cfg["holdings"] + cfg["candidates"]
    frames, raws, errors = {}, {}, []
    for t in universe:
        raw = raw_data.get(t)
        if raw is None:
            errors.append({"ticker": t, "error": "抓不到資料"})
            continue
        if asof is not None:
            raw = raw[raw.index <= asof]
        if len(raw) < MIN_BARS:
            errors.append({"ticker": t, "error": f"歷史資料不足（{len(raw)} 天）"})
            continue
        frames[t], raws[t] = adjusted(raw), raw

    rs_raw = {t: ind.rs_raw(df["Close"], bench["Close"]) for t, df in frames.items()}
    rs_pct = ind.percentile_rank(rs_raw) if rs_raw else {}
    w = cfg["weights"]
    pos_total = w["relative_strength"] + w["money_flow"] + w["trend"] + w["rebound"]

    results = {}
    for t, df in frames.items():
        raw = raws[t]
        limit = limit_state(cfg, t, raw)
        # 漲跌停鎖住那天成交量會很小（買不到 / 賣不掉），資金流沿用前一天的值
        mf_df = df.iloc[:-1] if limit else df
        comp = {
            "rs": rs_pct[t],
            "mf": ind.money_flow_score(mf_df),
            "trend": ind.trend_score(df),
            "rebound": ind.rebound_score(df),
            "risk": ind.risk_score(df),
        }
        # 正向四項加權後縮放到 (100 - risk 權重)，再加上 risk 權重 x (1 - 風險)
        positive = (w["relative_strength"] * comp["rs"] + w["money_flow"] * comp["mf"]
                    + w["trend"] * comp["trend"] + w["rebound"] * comp["rebound"]) / pos_total
        score = positive * (100 - w["risk"]) / 100 + w["risk"] * (100 - comp["risk"]) / 100
        c = df["Close"]
        rc = raw["Close"]
        bar = raw.iloc[-1]
        results[t] = {
            "score": round(float(score), 1),
            "components": {k: round(float(v), 1) for k, v in comp.items()},
            "price": charts._r(float(rc.iloc[-1])),
            "prev_close": charts._r(float(rc.iloc[-2])),
            "chg_1d_pct": round(float(rc.iloc[-1] / rc.iloc[-2] - 1) * 100, 2),
            "above_sma20": bool(c.iloc[-1] > ind.sma(c, 20).iloc[-1]),
            "above_sma50": bool(c.iloc[-1] > ind.sma(c, 50).iloc[-1]),
            "limit": limit,
            "data_date": raw.index[-1].date().isoformat(),
            "spark": [charts._r(float(x)) for x in rc.iloc[-30:]],
            "exit": {k: (charts._r(v) if v is not None else None) for k, v in ind.exit_levels(raw).items()},
            "bar": {"t": raw.index[-1].date().isoformat(), "o": charts._r(float(bar["Open"])),
                    "h": charts._r(float(bar["High"])), "l": charts._r(float(bar["Low"])),
                    "c": charts._r(float(bar["Close"])), "v": int(bar["Volume"])},
        }
    return results, errors


def reasons_for(r):
    c = r["components"]
    out = []
    if r.get("limit") == "up":
        out.append("漲停")
    elif r.get("limit") == "down":
        out.append("跌停")
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
        # 漲停鎖住實際上買不到，先列觀察
        return "watch" if r.get("limit") == "up" else "buy"
    if r["score"] >= 60:
        return "watch"
    return "none"


def reasons_dip(r):
    c = r["components"]
    out = []
    if r.get("limit") == "up":
        out.append("漲停")
    elif r.get("limit") == "down":
        out.append("跌停")
    if r.get("dd126") is not None:
        out.append(f"距半年高點 −{r['dd126'] * 100:.0f}%")
    if c["oversold"] >= 60:
        out.append("超賣")
    if c["reversal"] >= 70:
        out.append("止跌訊號")
    if c["mfturn"] >= 50:
        out.append("資金回流")
    if r.get("new_low"):
        out.append("創 20 日新低")
    if c["risk"] >= 70:
        out.append("波動大")
    return out


def holding_signal_dip(r):
    if r.get("new_low"):
        return "exit"  # 跌破前 20 天低點：止跌失敗
    if r.get("dd126") is not None and r["dd126"] < 0.05:
        return "take"  # 已經彈回半年高點附近
    if r["components"]["reversal"] >= 70:
        return "rebound"
    return "hold"


def candidate_signal_dip(r):
    if r.get("entry_ok"):
        return "watch" if r.get("limit") == "up" else "buy"
    if r["score"] >= 55:
        return "watch"
    return "none"


def dip_results(results, series, weights, asof=None):
    """用每檔的分項時間序列算跌深分數。asof=None 用最後一天。"""
    out = {}
    for t, r in results.items():
        s = series.get(t)
        if s is None or not len(s):
            continue
        row = s.iloc[-1] if asof is None else (s.loc[asof] if asof in s.index else None)
        if row is None:
            continue
        comp = {"depth": row["dip_depth"], "oversold": row["dip_oversold"],
                "reversal": row["dip_reversal"], "mfturn": row["dip_mfturn"], "risk": row["risk"]}
        if not all(np.isfinite(float(v)) for v in comp.values()):
            continue
        score = float(bt.strategy_score(row, "dip", weights))
        out[t] = {
            **{k: v for k, v in r.items() if k not in ("score", "components")},
            "score": round(score, 1),
            "components": {k: round(float(v), 1) for k, v in comp.items()},
            "entry_ok": bool(bt.entry_ok(row, "dip", score)),
            "dd126": round(float(row["dd126"]), 3) if np.isfinite(row["dd126"]) else None,
            "new_low": bool(row["new_low"]),
        }
    return out


# ───────── 歷史與汰弱留強 ─────────

def save_history(cfg, hist):
    days = sorted(hist["confirmed"])
    for d in days[:-HISTORY_KEEP_DAYS]:
        del hist["confirmed"][d]
    write_json_atomic(cfg["history_path"], hist)


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

def write_json_atomic(path, obj, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def swap_status(s, need):
    if s["streak"] >= need:
        return "confirmed" if s["confirmed_streak"] >= need else "provisional"
    return "building" if s["streak"] > 0 else None


def prev_scores(hist, date):
    """前一個收盤確認日的分數，用來算「比昨天多幾分」。"""
    days = [d for d in sorted(hist["confirmed"]) if d < date]
    return hist["confirmed"][days[-1]]["scores"] if days else {}


def build_book(kind, b, results, swaps, weakest, port, alerts, symmap, hist, date):
    need = b["swap_days"]
    prev = prev_scores(hist, date)
    hsig, csig, rfn = ((holding_signal, candidate_signal, reasons_for) if kind == "strong"
                       else (holding_signal_dip, candidate_signal_dip, reasons_dip))

    def base(t, r):
        p = prev.get(t)
        reasons = rfn(r)
        if kind == "strong" and r.get("extended"):
            reasons.append("漲多等回檔")
        if kind == "strong" and r.get("pullback_ok"):
            reasons.append("回檔到均線")
        return {"ticker": t, "name": b["names"].get(t, ""), "yahoo": symmap.get(t, t),
                "theme": b["themes"].get(t, ""), **{k: v for k, v in r.items() if k != "entry_ok"},
                "entry_ok": bool(r.get("entry_ok")),
                "score_prev": p, "score_chg": round(r["score"] - p, 1) if p is not None else None,
                "reasons": reasons, "alerts": alerts.get(t, [])}

    holdings, candidates, swap_list = [], [], []
    for t in b["holdings"]:
        if t not in results:
            continue
        r = results[t]
        sig = hsig(r)
        holdings.append({**base(t, r), "signal": sig, "signal_label": SIGNAL_LABELS[sig],
                         "weakest": t == weakest,
                         "source": "manual" if t in b["fixed"] else "auto"})
    for t in b["candidates"]:
        if t not in results:
            continue
        r = results[t]
        sig = csig(r)
        s = swaps.get(t)
        status = swap_status(s, need) if s else None
        if status in ("confirmed", "provisional") and (kind == "strong" or r.get("entry_ok")):
            sig = "swap_in"
            swap_list.append({"in": t, "out": s["out"], "gap": s["gap"],
                              "streak": s["streak"], "need": need, "status": status})
        candidates.append({**base(t, r), "signal": sig, "signal_label": SIGNAL_LABELS[sig],
                           "swap": ({"status": status, "streak": s["streak"], "need": need,
                                     "gap": s["gap"], "out": s["out"]} if status else None)})
    holdings.sort(key=lambda x: -x["score"])
    candidates.sort(key=lambda x: -x["score"])
    swap_list.sort(key=lambda x: -(x["gap"] or 0))
    if any(s["status"] == "confirmed" for s in swap_list):
        for h in holdings:
            if h["ticker"] == weakest and h["source"] == "auto":
                h["signal"], h["signal_label"] = "swap_out", SIGNAL_LABELS["swap_out"]
    return {
        "key": kind,
        "label": BOOKS[kind]["label"],
        "components_meta": BOOKS[kind]["components"],
        "target_count": b["target_count"],
        "open_slots": max(0, b["target_count"] - len(holdings)),
        "weakest_holding": weakest,
        "swaps": swap_list,
        # 自動組合的異動紀錄（新增、換股），最新的在前
        "portfolio_changes": list(reversed(port["changes"][-30:])),
        "holdings": holdings,
        "candidates": candidates,
    }


def build_output(cfg, books, results, errors, mode, session, now_utc, demo, price_time, bench_quote):
    need = cfg["swap_days"]
    data_date = max((r["data_date"] for r in results.values()), default=None)
    local = now_utc.astimezone(cfg["zone"])
    strong = books["strong"]
    return {
        "version": 3,
        "market": cfg["key"],
        "market_label": cfg["label"],
        "timezone": cfg["tz"],
        "tz_label": cfg["tz_label"],
        "currency": cfg["currency"],
        "quote_delay_minutes": cfg["quote_delay_minutes"],
        "trading_hours": f"{cfg['open']}–{cfg['trade_close']}",
        "demo": demo,
        "mode": mode,  # intraday = 盤中暫定；close = 收盤確認
        "mode_label": "盤中暫定" if mode == "intraday" else "收盤確認",
        "market_session": session,
        "generated_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "generated_at_local": local.strftime(f"%Y-%m-%d %H:%M {cfg['tz_label']}"),
        "generated_at_et": now_utc.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET"),
        "price_time": price_time,
        "data_date": data_date,
        # 網頁用：超過這個分鐘數沒更新就顯示「資料過期」
        "stale_after_minutes": 40 if mode == "intraday" else minutes_until_next_open(cfg, now_utc),
        "benchmark": cfg["benchmark"],
        "benchmark_name": cfg["names"].get(cfg["benchmark"], cfg["benchmark"]),
        "benchmark_quote": bench_quote,
        "core": ({"t": cfg["core"], "name": cfg["names"].get(cfg["core"], cfg["core"]), "weight": round(cfg["core_weight"] * 100),
                  "quote": bench_quote if cfg["core"] == cfg["benchmark"] else None} if cfg.get("core") else None),
        "weights": cfg["weights"],
        "exit_policy": cfg["exit_policy"],
        "swap_rule": {"gap": cfg["swap_gap"], "days": need},
        # 舊版網頁讀的欄位 = 強勢名單
        "weakest_holding": strong["weakest_holding"],
        "swaps": strong["swaps"],
        "portfolio_changes": strong["portfolio_changes"],
        "holdings": strong["holdings"],
        "candidates": strong["candidates"],
        # 強勢名單的內容就在上面幾個欄位，這裡只放說明；跌深名單完整放在 books.dip
        "books": {"strong": {k: v for k, v in strong.items()
                             if k not in ("swaps", "portfolio_changes", "holdings", "candidates")},
                  **{k: v for k, v in books.items() if k != "strong"}},
        "errors": errors,
    }


def chart_markers(t, hist, port):
    marks = []
    for c in port["changes"]:
        if c.get("action") == "add" and c.get("ticker") == t:
            marks.append({"t": c["date"], "kind": "add", "text": "選入"})
        elif c.get("action") == "swap" and c.get("in") == t:
            marks.append({"t": c["date"], "kind": "in", "text": "換入"})
        elif c.get("action") == "swap" and c.get("out") == t:
            marks.append({"t": c["date"], "kind": "out", "text": "換出"})
    prev = None
    for d, v in sorted(hist["confirmed"].items()):
        sig = v.get("signals", {}).get(t)
        if sig in ("buy", "exit") and sig != prev:
            marks.append({"t": d, "kind": sig, "text": "進場訊號" if sig == "buy" else "出場訊號"})
        prev = sig
    return marks


def score_line(hist, t):
    return [{"t": d, "v": v["scores"][t]} for d, v in sorted(hist["confirmed"].items()) if t in v["scores"]]


def weekly_bars(df):
    """週 K（給 3 年、5 年的長線圖），附 10 週線（約 50 日）、40 週線（約 200 日）。"""
    w = df.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(subset=["Close"])
    w = w.iloc[-262:]
    c = w["Close"]
    return {"t": [d.date().isoformat() for d in w.index], "o": charts._arr(w["Open"]), "h": charts._arr(w["High"]),
            "l": charts._arr(w["Low"]), "c": charts._arr(c), "v": [int(x) for x in w["Volume"].fillna(0)],
            "ma10": charts._arr(c.rolling(10).mean()), "ma40": charts._arr(c.rolling(40).mean())}


def fetch_weekly(cfg, tickers, demo, raw_data):
    """5 年日線（只給長線圖用，評分不用）；失敗就用手上的 2 年資料。"""
    if demo:
        return {t: raw_data[t] for t in tickers if t in raw_data}
    try:
        symmap = symbol_map(cfg, tickers, demo)
        got = yf_download(list(symmap.values()), period="5y", interval="1d")
        back = {v: k for k, v in symmap.items()}
        return {back.get(s, s): df for s, df in got.items()}
    except Exception:
        traceback.print_exc()
        return {}


def charts_have_weekly(key):
    f = next(iter(sorted((DATA_DIR / "charts" / key).glob("*.json"))), None)
    return f is None or "wk" in read_json(f, {})


def write_charts(cfg, raw_data, results, hist, port, demo, dhist=None, dport=None):
    folder = DATA_DIR / "charts" / cfg["key"]
    bench = raw_data[cfg["benchmark"]]["Close"]
    long_data = fetch_weekly(cfg, list(results), demo, raw_data)
    keep = set()
    for t in results:
        raw = raw_data.get(t)
        if raw is None or len(raw) < 60:
            continue
        chart, _ = charts.analyze(raw)
        chart["ticker"] = t
        chart["name"] = cfg["names"].get(t, "")
        chart["dividends"] = charts.dividend_dates(raw)[-8:]
        chart["markers"] = chart_markers(t, hist, port)
        chart["scores"] = score_line(hist, t)
        if dhist is not None:  # 跌深名單自己的分數和標記
            chart["dip"] = {"scores": score_line(dhist, t), "markers": chart_markers(t, dhist, dport)}
        # 相對強弱線：個股 ÷ 大盤，第一天設成 100。往上 = 比大盤強
        idx = pd.to_datetime(chart["t"])
        ratio = raw["Close"].reindex(idx) / bench.reindex(idx).ffill()
        ratio = ratio / ratio.dropna().iloc[0] * 100 if ratio.notna().any() else ratio
        chart["rs"] = [charts._r(float(x), 2) for x in ratio.to_numpy(float)]
        lraw = long_data.get(t)
        lraw = lraw if lraw is not None and len(lraw) > len(raw) else raw
        chart["wk"] = weekly_bars(lraw)
        chart["demo"] = demo
        name = f"{t}.json"
        keep.add(name)
        write_json_atomic(folder / name, chart, compact=True)
    for f in folder.glob("*.json"):  # 已經不在名單裡的代號
        if f.name not in keep:
            f.unlink()


def write_intraday(cfg, frames, results, now_utc):
    """把當天各股的 5 分鐘線對齊到同一條時間軸。時間是「當地時鐘」的秒數，圖上直接顯示當地時間。"""
    times = sorted(set().union(*[set(df.index) for df in frames.values()])) if frames else []
    if not times:
        return None
    pos = {ts: i for i, ts in enumerate(times)}
    series = {}
    for t, df in frames.items():
        if t not in results:
            continue
        c = [None] * len(times)
        vw = [None] * len(times)
        vwap = charts.vwap(df)
        for ts, px, w in zip(df.index, df["Close"].to_numpy(float), vwap.to_numpy(float)):
            c[pos[ts]] = charts._r(px)
            vw[pos[ts]] = charts._r(w)
        series[t] = {"c": c, "vwap": vw}
    wall = [int(ts.replace(tzinfo=timezone.utc).timestamp()) if ts.tzinfo is None
            else int(ts.tz_localize(None).replace(tzinfo=timezone.utc).timestamp()) for ts in times]
    out = {"market": cfg["key"], "date": times[-1].date().isoformat(),
           "generated_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
           "t": wall, "series": series}
    write_json_atomic(DATA_DIR / "intraday" / f"{cfg['key']}.json", out, compact=True)
    return times[-1]


def update_markets_index(cfg, out):
    path = DATA_DIR / "markets.json"
    idx = read_json(path, {"markets": []})
    entry = {"key": cfg["key"], "label": cfg["label"], "file": f"data/{cfg['key']}.json",
             "timezone": cfg["tz"], "open": cfg["open"], "close": cfg["trade_close"],
             "generated_at": out["generated_at"], "mode": out["mode"],
             "mode_label": out["mode_label"], "market_session": out["market_session"],
             "stale_after_minutes": out["stale_after_minutes"], "demo": out["demo"]}
    others = [m for m in idx["markets"] if m["key"] != cfg["key"]]
    order = list(MARKETS)
    idx["markets"] = sorted(others + [entry], key=lambda m: order.index(m["key"]))
    write_json_atomic(path, idx)


# ───────── 主程式 ─────────

def run_market(cfg, args, now_utc):
    key = cfg["key"]
    tag = f"[{cfg['label']}]"
    if args.demo:  # 模擬資料的狀態另外存，不會汙染真的紀錄
        for k in ("history_path", "portfolio_path"):
            cfg[k] = cfg[k].with_name(cfg[k].stem + "_demo.json")
    now = now_utc.astimezone(cfg["zone"])
    session = market_session(cfg, now)
    hist = read_json(cfg["history_path"], {"confirmed": {}})
    tickers_all = list(dict.fromkeys(cfg["fixed"] + cfg["pool"]))
    if ext_session(cfg, now) and (args.mode == "auto" or args.demo):
        try:
            ext = write_ext(cfg, tickers_all, now, now_utc, args.demo)
            if ext:
                print(f"{tag} {ext['label']}價格 {len(ext['items'])} 檔")
        except Exception:
            print(f"{tag} 盤前盤後價格抓取失敗（不影響評分）", file=sys.stderr)
            traceback.print_exc()
    if not args.demo:  # 基本面與大盤儀表板（只顯示，不進評分）
        try:
            import fundamentals
            fundamentals.run(key, tickers_all, yf_download, now_utc, dash=True)
        except Exception:
            print(f"{tag} 基本面資料更新失敗（不影響評分）", file=sys.stderr)
            traceback.print_exc()
    if not args.demo:  # 全市場類股輪動（休市時才更新，只顯示，不進評分）
        try:
            import rotation
            rotation.run(key, cfg, yf_download, now_utc, in_session=(session == "open"))
        except Exception:
            print(f"{tag} 類股輪動更新失敗（不影響評分）", file=sys.stderr)
            traceback.print_exc()
    if not args.demo:  # 期權資料（只顯示，不進評分）
        try:
            import options
            options.run(key, tickers_all, now_utc, in_session=(session == "open"))
        except Exception:
            print(f"{tag} 期權資料更新失敗（不影響評分）", file=sys.stderr)
            traceback.print_exc()
    # 剛升級到兩套名單時，跌深紀錄或績效檔還不存在，就算休市也先跑一次收盤確認把它們產生出來
    upgraded = (cfg["history_path"].with_name(cfg["history_path"].stem + "_dip.json").exists()
                and read_json(DATA_DIR / "perf" / f"{key}.json", {}).get("bt_v") == BT_V
                and charts_have_weekly(key))
    if args.mode == "auto" and not args.demo and not args.backfill and hist["confirmed"] \
            and upgraded and nothing_to_do(cfg, now, hist) \
            and (DATA_DIR / "sim" / f"{key}.json").exists() \
            and (DATA_DIR / "sim" / f"{key}_cs.json").exists() \
            and not config_newer(key):  # 模擬倉還沒建、或剛改過設定檔：休市也跑一次
        print(f"{tag} 休市中，最近一次收盤已確認，這次不更新。")
        return 0

    port = read_json(cfg["portfolio_path"], {"auto": [], "changes": []})
    apply_portfolio(cfg, port)
    tickers = list(dict.fromkeys([cfg["benchmark"]] + cfg["fixed"] + cfg["pool"]))
    symmap = symbol_map(cfg, tickers, args.demo)
    if args.demo:
        raw_data = fetch_demo(tickers, end=now.date(), key=key)
    else:
        by_sym = fetch_daily(list(symmap.values()))
        raw_data = {t: by_sym[s] for t, s in symmap.items() if s in by_sym}
        if key == "tw" and session == "open":
            try:  # 永豐金即時報價（有填金鑰才會用），蓋掉 Yahoo 延遲 20 分鐘的今天這根
                n = twrt.patch(raw_data, now.date().isoformat())
                if n:
                    cfg["quote_delay_minutes"] = 0
                    print(f"{tag} 永豐金即時報價：更新 {n} 檔")
            except Exception:
                traceback.print_exc()
    if cfg["benchmark"] not in raw_data:
        print(f"{tag} 錯誤：抓不到 {cfg['benchmark']}，可能是網路問題，這次不更新。", file=sys.stderr)
        return 1
    bench_last = raw_data[cfg["benchmark"]].index[-1].date()

    mode = args.mode
    if mode == "auto":
        # 盤中時段但最新資料不是今天 = 休市（國定假日、颱風假），改跑收盤確認
        mode = "intraday" if session == "open" and bench_last == now.date() else "close"
    if session == "open" and bench_last != now.date():
        session = "holiday"

    if mode == "close" and date_lt(bench_last, expected_close_date(cfg, now)):
        hist["holiday_checked"] = expected_close_date(cfg, now)  # 資料沒有該日 = 國定假日

    results, errors = score_universe(raw_data, cfg)
    date = bench_last.isoformat()

    # 每檔的分項時間序列：給跌深分數、回檔判斷和回測用
    bench_adj = adjusted(raw_data[cfg["benchmark"]])["Close"]
    series = {}
    for t in results:
        try:
            series[t] = bt.component_series(adjusted(raw_data[t]), bench_adj)
        except Exception:
            pass
    for t, r in results.items():
        s = series.get(t)
        if s is not None:
            row = s.iloc[-1]
            r["extended"] = bool(row["extended"])
            r["pullback_ok"] = bool(bt.entry_ok(row, "pullback", r["score"]) and r["components"]["trend"] >= 60)
    dres = dip_results(results, series, cfg["weights"])

    backfill = args.backfill or (0 if hist["confirmed"] else 5)  # 新市場第一次跑：自動補算 5 天
    bf_dates = list(raw_data[cfg["benchmark"]].index[-(backfill + 1):-1]) if backfill else []
    bf_strong = {d.date().isoformat(): score_universe(raw_data, cfg, asof=d)[0] for d in bf_dates}

    # 跌深名單有自己的持股和歷史；沒有你指定的固定持股
    dcfg = dict(cfg, fixed=[], pool=list(dict.fromkeys(cfg["fixed"] + cfg["pool"])),
                history_path=cfg["history_path"].with_name(cfg["history_path"].stem + "_dip.json"),
                portfolio_path=cfg["portfolio_path"].with_name(cfg["portfolio_path"].stem + "_dip.json"))
    dhist = read_json(dcfg["history_path"], {"confirmed": {}})
    dport = read_json(dcfg["portfolio_path"], {"auto": [], "changes": []})
    apply_portfolio(dcfg, dport)
    dbackfill = args.backfill or (0 if dhist["confirmed"] else 5)
    bf_dip = {}
    if dbackfill:
        for d in list(raw_data[cfg["benchmark"]].index[-(dbackfill + 1):-1]):
            bf_dip[d.date().isoformat()] = dip_results(bf_strong.get(d.date().isoformat()) or results,
                                                       series, cfg["weights"], asof=d)

    books = {}
    for kind, b, h, pt, res, bf, gate in (("strong", cfg, hist, port, results, bf_strong, False),
                                          ("dip", dcfg, dhist, dport, dres, bf_dip, True)):
        name = f"{tag}{BOOKS[kind]['label']}"
        added = fill_portfolio(b, pt, res, date, gate=gate)
        if added:
            print(f"{name} 自動選入 {len(added)} 檔：{', '.join(added)}")
        for d, r in sorted(bf.items()):
            record_confirmed(h, d, r, b["holdings"])
        if bf:
            print(f"{name} 補算 {len(bf)} 天")
        if mode == "close":
            record_confirmed(h, date, res, b["holdings"])
            auto_swaps, _ = swap_analysis(h, b, only=set(pt["auto"]))
            change = execute_swap(b, pt, auto_swaps, res, date, gate=gate)
            if change:
                print(f"{name} 自動換股：{change['out']} → {change['in']}（差 {change['gap']} 分）")
                # 收盤後已換股，今天的紀錄改成新組合，訊號也重算
                fill_portfolio(b, pt, res, date, gate=gate)
                h["confirmed"][date]["holdings"] = [t for t in b["holdings"] if t in res]
            swaps, weakest = swap_analysis(h, b)
        else:
            today = (date, {t: r["score"] for t, r in res.items()},
                     [t for t in b["holdings"] if t in res])
            swaps, weakest = swap_analysis(h, b, today=today)
        books[kind] = (b, h, pt, res, swaps, weakest)

    # 走勢圖提示（只當參考，不進評分）
    alerts = {}
    for t in results:
        try:
            _, alerts[t] = charts.analyze(raw_data[t])
        except Exception:
            alerts[t] = []

    # 今日 5 分鐘線：盤中，或當天收盤後
    price_time = None
    if mode == "intraday" or bench_last == now.date():
        try:
            want = {t: symmap[t] for t in results if t in symmap}
            frames = (demo_intraday({t: raw_data[t] for t in want}, cfg, now) if args.demo
                      else _intraday_raw(want, cfg))
            last_ts = write_intraday(cfg, frames, results, now_utc)
            if last_ts is not None and mode == "intraday":
                price_time = (last_ts + timedelta(minutes=5)).strftime("%H:%M")
        except Exception as e:
            print(f"{tag} 今日走勢抓取失敗（不影響評分）：{e}", file=sys.stderr)
    if price_time is None:
        price_time = "收盤" if mode == "close" else now.strftime("%H:%M")

    braw = raw_data[cfg["benchmark"]]["Close"]
    bench_quote = {"price": charts._r(float(braw.iloc[-1])),
                   "chg_1d_pct": round(float(braw.iloc[-1] / braw.iloc[-2] - 1) * 100, 2)}
    # 大盤多空：收盤在 200 日線上＝多頭；50 日線用來看短線轉弱
    for n in (50, 200):
        if len(braw) >= n:
            ma = float(braw.iloc[-n:].mean())
            bench_quote[f"sma{n}"] = charts._r(ma)
            bench_quote[f"vs_sma{n}_pct"] = round((float(braw.iloc[-1]) / ma - 1) * 100, 1)
    built = {kind: build_book(kind, b, res, swaps, weakest, pt, alerts, symmap, h, date)
             for kind, (b, h, pt, res, swaps, weakest) in books.items()}
    out = build_output(cfg, built, results, errors, mode, session, now_utc, args.demo,
                       price_time, bench_quote)

    for kind, (b, h, pt, res, _, _) in books.items():
        if mode == "close":
            bk = built[kind]
            h["confirmed"][date]["signals"] = {i["ticker"]: i["signal"]
                                               for i in bk["holdings"] + bk["candidates"]}
        if mode == "close" or (backfill if kind == "strong" else dbackfill):
            save_history(b, h)
        write_json_atomic(b["portfolio_path"], pt)

    write_json_atomic(DATA_DIR / f"{key}.json", out)
    if key == "us":
        write_json_atomic(DOCS / "data.json", out)  # 舊網頁快取還在讀這個檔
    folder = DATA_DIR / "charts" / key
    if mode == "close" or not folder.exists() or not any(folder.glob("*.json")):
        write_charts(cfg, raw_data, results, hist, port, args.demo, dhist, dport)
    perf_path = DATA_DIR / "perf" / f"{key}.json"
    if mode == "close" or not perf_path.exists():
        old = read_json(perf_path, {})
        if old.get("data_date") != date or "exits" not in old or old.get("bt_v") != BT_V or args.demo:
            try:
                write_perf(cfg, dcfg, raw_data, results, hist, dhist, date, now_utc, args.demo)
            except Exception:
                print(f"{tag} 回測失敗（不影響評分）：", file=sys.stderr)
                traceback.print_exc()
                # 留一個空的績效檔，免得休市時每 15 分鐘重跑
                write_json_atomic(perf_path, {"market": key, "data_date": date, "backtest": {}, "variants": [], "scorecard": [], "exits": [], "live": {},
                                              "note": "這次回測失敗，下一個收盤日會重算。"})
    if not args.demo:
        try:  # 模擬倉：照程式選股名單下單記帳
            sim.update(cfg, raw_data, hist, DATA_DIR, cfg["history_path"].parent, out["generated_at"])
        except Exception:
            print(f"{tag} 模擬倉更新失敗（不影響評分）：", file=sys.stderr)
            traceback.print_exc()
        try:  # 核心＋衛星：一半槓桿 ETF、一半程式選股
            sim.update_cs(cfg, raw_data, hist, DATA_DIR, cfg["history_path"].parent, out["generated_at"], fetch_daily)
        except Exception:
            print(f"{tag} 核心＋衛星模擬帳戶更新失敗（不影響評分）：", file=sys.stderr)
            traceback.print_exc()
    update_markets_index(cfg, out)
    print(f"{tag} {out['mode_label']}｜資料日期 {date}｜強勢持股 {len(out['holdings'])} 檔、"
          f"跌深持股 {len(built['dip']['holdings'])} 檔｜換股訊號 {len(out['swaps'])}"
          f" + {len(built['dip']['swaps'])} 個｜失敗 {len(errors)} 檔")
    return 0


def write_perf(cfg, dcfg, raw_data, results, hist, dhist, date, now_utc, demo):
    """回測三種選股方式（過去一年），加上兩套名單的實際紀錄。"""
    universe = [t for t in dict.fromkeys(cfg["fixed"] + cfg["pool"]) if t in results]
    frames = {t: adjusted(raw_data[t]) for t in universe}
    bench = adjusted(raw_data[cfg["benchmark"]])["Close"]
    rules = sys.modules[__name__]
    costs = bt.COSTS.get(cfg["key"], bt.COSTS["us"])
    p = bt.panel(frames, bench)
    books = {"momentum": cfg, "pullback": cfg, "dip": dcfg}
    tests, variants, best, preps = {}, [], None, {}
    for strat, b in books.items():
        bb = dict(b, holdings=list(b["fixed"]), candidates=list(b["pool"]))
        prep = bt.prepare(frames, bench, bb, strat, p=p)
        preps[strat] = prep
        # 主曲線：設定檔的檔數、含交易成本、不加大盤濾網
        res = bt.simulate(prep, bb, rules, costs=costs)
        if not res:
            continue
        tests[strat] = res
        # 其他組合：集中持股、大盤濾網
        for n in sorted({5, 8, b["target_count"]}):
            for regime in (False, True):
                r = res if (n == b["target_count"] and not regime) else bt.simulate(
                    prep, bb, rules, top_n=n, regime=regime, costs=costs)
                if not r:
                    continue
                row = {k: v for k, v in r.items() if k not in ("curve", "final_holdings")}
                row["strategy"] = strat
                variants.append(row)
                if best is None or (r["cagr_pct"] or -999) > (best["cagr_pct"] or -999):
                    best = dict(r, strategy=strat)
    variants.sort(key=lambda r: -(r["cagr_pct"] if r["cagr_pct"] is not None else -999))
    # 出場方式比較：強勢名單＋不同停損規則
    exits = []
    if "momentum" in preps:
        bb = dict(cfg, holdings=list(cfg["fixed"]), candidates=list(cfg["pool"]))
        for rule in (None, "sma50", "atr3", "pct8"):
            r = tests.get("momentum") if rule is None else bt.simulate(preps["momentum"], bb, rules, costs=costs, exit_rule=rule)
            if r:
                exits.append({"rule": rule or "none", **{k: v for k, v in r.items() if k not in ("curve", "final_holdings")}})
    first = next(iter(tests.values()), None)
    dates = [d for d, _ in first["curve"]] if first else []
    out = {
        "market": cfg["key"],
        "data_date": date,
        "generated_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "demo": demo,
        "benchmark": cfg["benchmark"],
        "benchmark_name": cfg["names"].get(cfg["benchmark"], cfg["benchmark"]),
        "costs": costs,
        "backtest": tests,
        "backtest_benchmark": bt.bench_curve(bench, dates) if first else None,
        "variants": variants,
        "best": best,
        "goal": bt.goal_curve(dates),
        "scorecard": bt.scorecard(preps),
        "exits": exits,
        "bt_v": BT_V,
        "live": {"strong": bt.live_curve(hist, frames, bench, cfg["target_count"]),
                 "dip": bt.live_curve(dhist, frames, bench, dcfg["target_count"])},
        "note": f"回測假設每個收盤確認日依規則換股、持股等權重、空位放現金；每次買賣扣 {costs['buy'] * 100:.2f}%"
                f"（賣出 {costs['sell'] * 100:.2f}%）的手續費、稅和滑價。過去績效不代表未來，只供研究參考。",
    }
    write_json_atomic(DATA_DIR / "perf" / f"{cfg['key']}.json", out, compact=True)


def main():
    ap = argparse.ArgumentParser(description="股票翻倍計畫掃描器")
    ap.add_argument("--market", choices=["all", *MARKETS], default="all")
    ap.add_argument("--mode", choices=["auto", "intraday", "close"], default="auto")
    ap.add_argument("--backfill", type=int, default=0, metavar="N",
                    help="補算過去 N 個交易日的收盤分數")
    ap.add_argument("--demo", action="store_true", help="用模擬資料，不連網")
    args = ap.parse_args()

    migrate_state()
    cfgs = load_config()
    now_utc = datetime.now(timezone.utc)
    rc = 0
    for key, cfg in cfgs.items():
        if args.market not in ("all", key):
            continue
        try:
            rc |= run_market(cfg, args, now_utc)
        except Exception:
            print(f"[{cfg['label']}] 執行失敗，另一個市場照常：", file=sys.stderr)
            traceback.print_exc()
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
