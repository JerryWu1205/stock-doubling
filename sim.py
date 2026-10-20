"""模擬倉：照「程式選股」名單真的下單，記錄每筆買賣的時間、股數、價格、金額、手續費，算每天的淨值。

美股起始 50,000 美元、台股 1,000,000 台幣。每個收盤確認日用當天收盤價成交：
- 名單裡沒有了（被換掉）就賣；固定出場規則觸發了（美股跌破 50 日線）也賣。
- 名單裡有、手上沒有、也沒觸發出場的就買，每檔目標金額＝淨值 ÷ 持股檔數，現金不夠就買到現金用完。
- 手上已有的不會為了等權重去加減碼（少換手、少成本）。
手續費、稅照 backtest.COSTS，台股手續費最低 20 元；股利不計。這是模擬，不是實際下單。
帳本存在 state/<市場>/sim.json，網頁讀 docs/data/sim/<市場>.json。"""
import json
import math
from datetime import date as _date

import numpy as np
import pandas as pd

import backtest as bt
import indicators as ind

START_CASH = {"us": 50000.0, "tw": 1000000.0}
CURRENCY = {"us": "USD", "tw": "TWD"}
START_DATE = "2026-10-16"  # 第一個成交日（這天收盤後確認的名單）
VERSION = 3  # 2：持股上限改 5 檔；3：台股改成一半 0050 核心＋4 檔選股。版本變了就從 10/16 收盤重建


def _fee(key, amount, side):
    c = bt.COSTS.get(key, bt.COSTS["us"])[side]
    f = amount * c
    return max(f, 20.0) if key == "tw" else f


def _exit_rule(cfg):
    """出場規則和網頁「我的」、Telegram 持股報告共用（config.toml 的 [exit]），固定不跟著回測每天換：
    跌破成本 −stop_pct% 全賣；跌破吊燈停損賣一半（一筆持股只賣一次）；在 50 日線下不買進、不加碼。"""
    return dict(cfg.get("exit_policy") or {"stop_pct": 8.0})


def _levels(df, d):
    """d 那天收盤的 (收盤價, 吊燈停損, 50 日線)，和網頁的退場參考價同一個算法（indicators.exit_levels）。"""
    if df is None:
        return None, None, None
    s = df.loc[:d]
    if len(s) < 2 or s.index[-1] != d:
        return None, None, None
    lv = ind.exit_levels(s)
    return float(s["Close"].iloc[-1]), lv["trail"], lv["sma50"]


def _exit_hit(rule, df, d, pos):
    """d 那天收盤要不要賣：回傳 ("all"|"half"|None, 原因)。pos=None 時只看能不能買（50 日線下不買）。"""
    c, trail, sma50 = _levels(df, d)
    if c is None:
        return None, None
    if pos is None:
        return ("nobuy", "在 50 日線下") if sma50 is not None and c < sma50 else (None, None)
    stop = rule.get("stop_pct", 8.0)
    if c < pos["cost"] * (1 - stop / 100):
        return "all", f"跌破成本 −{stop:g}%"
    if trail is not None and c < trail and not pos.get("halved"):
        return "half", "跌破吊燈停損，賣一半"
    return None, None


def _top(day, slots, themes, cap):
    """當天名單裡分數最高的 slots 檔（每個主題最多 cap 檔）。名單本來就不超過 slots 檔時照原樣。"""
    hold = list(day["holdings"])
    if len(hold) <= slots:
        return hold
    sc, cnt, out = day.get("scores") or {}, {}, []
    for t in sorted(hold, key=lambda t: -sc.get(t, -1)):
        th = themes.get(t, "")
        if len(out) < slots and cnt.get(th, 0) < cap:
            out.append(t)
            cnt[th] = cnt.get(th, 0) + 1
    return out


def _px(df, d):
    if df is None or d not in df.index:
        return None
    v = df.at[d, "Close"]
    return float(v) if pd.notna(v) and v > 0 else None


def _new_ledger(key):
    return {"version": VERSION, "market": key, "currency": CURRENCY[key], "start_cash": START_CASH[key],
            "start_date": None, "cash": START_CASH[key], "positions": {}, "trades": [], "nav": [],
            "bench_units": None, "last": None}


def step(L, key, d, holdings, raw, bench, rule, slots, names, extra=0.0, frac=1.0):
    """處理一個收盤確認日 d（字串 YYYY-MM-DD）。
    extra：帳上另外的部位市值（核心＋衛星的槓桿 ETF），算淨值用；frac：選股這一半占淨值的比例。"""
    ts = pd.Timestamp(d)
    pos = L["positions"]
    # 1. 賣：被換出名單，或觸發出場規則
    for t in list(pos):
        p = pos[t]
        df = raw.get(t)
        px = _px(df, ts)
        if px is None:
            continue
        part, reason = None, None
        if t not in holdings:
            part, reason = "all", "換出名單"
        else:
            part, reason = _exit_hit(rule, df, ts, p)
        if not part:
            continue
        sh = p["sh"] if part == "all" else p["sh"] // 2
        if sh <= 0:  # 只剩 1 股，賣一半＝不賣
            p["halved"] = True
            continue
        gross = sh * px
        fee = _fee(key, gross, "sell")
        net = gross - fee
        L["cash"] += net
        ct = p["cost_total"] * sh / p["sh"]
        pnl = net - ct
        L["trades"].append({"date": d, "side": "賣", "t": t, "name": names.get(t, ""), "sh": sh,
                            "px": round(px, 4), "amount": round(gross, 2), "fee": round(fee, 2), "reason": reason,
                            "pnl": round(pnl, 2), "pnl_pct": round(pnl / ct * 100, 2),
                            "buy_date": p["date"],
                            "days": int(np.busday_count(_date.fromisoformat(p["date"]), _date.fromisoformat(d)))})
        if part == "all":
            del pos[t]
        else:
            p["sh"] -= sh
            p["cost_total"] = round(p["cost_total"] - ct, 2)
            p["halved"] = True
    # 2. 買：名單裡有、手上沒有、沒觸發出場
    nav = L["cash"] + sum(p["sh"] * (_px(raw.get(t), ts) or p["last"]) for t, p in pos.items())
    slot = (nav + extra) * frac / max(slots, 1)
    for t in holdings:
        if t in pos:
            continue
        df = raw.get(t)
        px = _px(df, ts)
        if px is None or _exit_hit(rule, df, ts, None)[0]:
            continue
        budget = min(slot, L["cash"])
        sh = math.floor(budget / (px * (1 + bt.COSTS.get(key, bt.COSTS["us"])["buy"])))
        while sh > 0 and sh * px + _fee(key, sh * px, "buy") > L["cash"]:
            sh -= 1
        if sh <= 0:
            continue
        gross = sh * px
        fee = _fee(key, gross, "buy")
        L["cash"] -= gross + fee
        pos[t] = {"sh": sh, "cost": round((gross + fee) / sh, 4), "cost_total": round(gross + fee, 2),
                  "date": d, "last": px}
        L["trades"].append({"date": d, "side": "買", "t": t, "name": names.get(t, ""), "sh": sh,
                            "px": round(px, 4), "amount": round(gross, 2), "fee": round(fee, 2),
                            "reason": "建倉" if L["start_date"] == d else
                            "停損後站回 50 日線買回" if any(x["t"] == t and x["side"] == "賣" and x["reason"] != "換出名單"
                                                       for x in L["trades"][-100:]) else "選入名單"})
    # 3. 收盤淨值
    for t, p in pos.items():
        p["last"] = _px(raw.get(t), ts) or p["last"]
    nav = L["cash"] + sum(p["sh"] * p["last"] for p in pos.values()) + extra
    b = _px(bench, ts)
    if L["bench_units"] is None and b:
        L["bench_units"] = L["start_cash"] / b
    L["nav"].append([d, round(nav, 2), round(L["bench_units"] * b, 2) if b and L["bench_units"] else None])
    L["last"] = d


def _since(nav, cutoff):
    """cutoff 之前最後一筆淨值；沒有就用起始那筆。"""
    prev = [v for d, v, _ in nav if d < cutoff]
    return prev[-1] if prev else None


def summarize(L, key, raw, names, rule, slots, now_iso):
    nav = L["nav"]
    start = L["start_cash"]
    last_nav = nav[-1][1] if nav else start
    # 盤中估值：用最新價（收盤確認後就是收盤價）
    rows, mv = [], 0.0
    for t, p in sorted(L["positions"].items()):
        df = raw.get(t)
        px = float(df["Close"].dropna().iloc[-1]) if df is not None and len(df["Close"].dropna()) else p["last"]
        val = p["sh"] * px
        mv += val
        rows.append({"t": t, "name": names.get(t, ""), "sh": p["sh"], "cost": p["cost"], "px": round(px, 4),
                     "value": round(val, 2), "pnl": round(val - p["cost_total"], 2),
                     "pnl_pct": round((val / p["cost_total"] - 1) * 100, 2), "date": p["date"]})
    for r in rows:
        r["weight"] = round(r["value"] / (mv + L["cash"]) * 100, 1) if mv + L["cash"] else 0
    live = L["cash"] + mv
    today = nav[-1][0] if nav else None

    def ret(base):
        return round((last_nav / base - 1) * 100, 2) if base else None

    week_cut = month_cut = None
    if today:
        dt = _date.fromisoformat(today)
        week_cut = (dt - pd.Timedelta(days=dt.weekday())).isoformat()  # 本週一
        month_cut = dt.replace(day=1).isoformat()
    prev_day = nav[-2][1] if len(nav) >= 2 else start
    sells = [x for x in L["trades"] if x["side"] == "賣"]
    wins = [x for x in sells if x["pnl"] > 0]
    days = max(len(nav) - 1, 0)
    bench_last = nav[-1][2] if nav and nav[-1][2] else None
    goal = start * 2 ** (days / 252)
    peak, mdd = start, 0.0
    for _, v, _ in nav:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    return {
        "market": key, "currency": L["currency"], "generated_at": now_iso, "start_date": L["start_date"],
        "start_cash": start, "data_date": today, "exit_rule": "policy", "exit_policy": rule, "slots": slots,
        "nav": last_nav, "live_nav": round(live, 2), "cash": round(L["cash"], 2), "max_dd": round(mdd * 100, 2),
        "ret": {"day": ret(prev_day), "week": ret(_since(nav, week_cut) or start) if today else None,
                "month": ret(_since(nav, month_cut) or start) if today else None, "total": ret(start)},
        "bench": {"nav": bench_last, "total": round((bench_last / start - 1) * 100, 2) if bench_last else None},
        "goal": {"nav": round(goal, 2), "total": round((goal / start - 1) * 100, 2), "days": days},
        "realized": {"pnl": round(sum(x["pnl"] for x in sells), 2), "n": len(sells),
                     "win_pct": round(len(wins) / len(sells) * 100) if sells else None,
                     "avg_pct": round(sum(x["pnl_pct"] for x in sells) / len(sells), 2) if sells else None,
                     "fees": round(sum(x["fee"] for x in L["trades"]), 2)},
        "positions": sorted(rows, key=lambda r: -r["value"]),
        "curve": nav,
        "trades": list(reversed(L["trades"][-200:])),
        "note": "照程式選股名單在收盤確認日用收盤價成交，每檔目標＝淨值 ÷ 持股檔數；手續費、稅、滑價照績效頁回測，"
                "台股手續費最低 20 元，股利不計。出場規則和網頁「我的」、Telegram 持股報告同一套：跌破成本 −8% 全賣、跌破吊燈停損賣一半、50 日線下不買進。模擬帳戶，不是實際下單，不是投資建議。",
    }


def core_step(L, key, d, raw, sym, weight, names):
    """核心部位（台股 0050）：第一個收盤日用收盤價買到淨值的 weight，之後一直持有、不看任何濾網、不調整。
    回傳核心部位的收盤市值，給 step() 當 extra。"""
    ts = pd.Timestamp(d)
    px = _px(raw.get(sym), ts)
    c = L.get("core")
    if c is None and px:
        nav = L["cash"] + sum(p["sh"] * (_px(raw.get(t), ts) or p["last"]) for t, p in L["positions"].items())
        budget = min(nav * weight, L["cash"])
        sh = math.floor(budget / (px * (1 + bt.COSTS.get(key, bt.COSTS["us"])["buy"])))
        while sh > 0 and sh * px + _fee(key, sh * px, "buy") > L["cash"]:
            sh -= 1
        if sh > 0:
            gross = sh * px
            fee = _fee(key, gross, "buy")
            L["cash"] -= gross + fee
            c = L["core"] = {"sh": sh, "cost": round((gross + fee) / sh, 4), "cost_total": round(gross + fee, 2),
                             "date": d, "last": px}
            L["trades"].append({"date": d, "side": "買", "t": sym, "name": names.get(sym, ""), "sh": sh,
                                "px": round(px, 4), "amount": round(gross, 2), "fee": round(fee, 2),
                                "reason": f"核心 {round(weight * 100)}%（固定持有，不換股）"})
    if c is None:
        return 0.0
    c["last"] = px or c["last"]
    return c["sh"] * c["last"]


def update(cfg, raw_data, hist, data_dir, state_dir, now_iso):
    key = cfg["key"]
    path = state_dir / "sim.json"
    try:
        L = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        L = _new_ledger(key)
    if L.get("version") != VERSION:  # 規則改了：從頭重建
        L = _new_ledger(key)
    rule = _exit_rule(cfg)
    slots = cfg["target_count"]
    names = cfg["names"]
    bench = raw_data.get(cfg["benchmark"])
    raw = {t: df for t, df in raw_data.items()}
    core, cw = cfg.get("core"), cfg.get("core_weight", 0.5)  # 台股：一半 0050 核心（config.toml 的 core）
    for d in sorted(hist["confirmed"]):
        if d < START_DATE or (L["last"] and d <= L["last"]):
            continue
        if L["start_date"] is None:
            L["start_date"] = d
        picks = _top(hist["confirmed"][d], slots, cfg["themes"], cfg["max_per_theme"])
        if core:
            cv = core_step(L, key, d, raw, core, cw, names)
            step(L, key, d, [t for t in picks if t != core], raw, bench, rule, slots, names, extra=cv, frac=1 - cw)
        else:
            step(L, key, d, picks, raw, bench, rule, slots, names)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(L, ensure_ascii=False), encoding="utf-8")
    view = dict(L, positions={**L["positions"], **({core: L["core"]} if core and L.get("core") else {})})
    out = summarize(view, key, raw, names, rule, slots, now_iso)
    if core:
        out["core"] = {"t": core, "name": names.get(core, ""), "weight": round(cw * 100), "held": bool(L.get("core"))}
        out["note"] = (f"一半放 {core} {names.get(core, '')}（核心，一直持有、不換股、不看濾網）；另一半照程式選股名單分數最高的 "
                       f"{slots} 檔，每檔＝淨值 × {round((1 - cw) * 100)}% ÷ {slots}，在收盤確認日用收盤價成交；出場規則：跌破成本 −8% 全賣、跌破吊燈停損賣一半、50 日線下不買進，換出名單也賣。"
                       "手續費、稅、滑價照績效頁回測，台股手續費最低 20 元，股利不計。模擬帳戶，不是實際下單，不是投資建議。")
    (data_dir / "sim").mkdir(parents=True, exist_ok=True)
    tmp = data_dir / "sim" / f"{key}.json.tmp"
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(data_dir / "sim" / f"{key}.json")
    return out


# ───────── 核心＋衛星：一半槓桿 ETF（有趨勢、總經不是紅燈才拿），一半程式選股 ─────────
CS_LEV = {"us": ("SOXL", "SOXX"), "tw": ("00631L", "0050")}  # (槓桿 ETF, 看趨勢的原型)
CS_NAMES = {"SOXL": "三倍做多半導體ETF", "SOXX": "費城半導體ETF", "00631L": "元大台灣50正2", "0050": "元大台灣50"}
CS_REFRESH_MIN = 60  # 沒有新收盤日時，槓桿 ETF 和總經資料多久重抓一次（盤中估值用）


def _trend_on(df, ts):
    """收盤站上 50 日線，而且 50 日線在 200 日線上面（和績效頁槓桿 ETF 的趨勢規則一樣）。"""
    if df is None:
        return False
    c = df["Close"].loc[:ts].dropna()
    if len(c) < 200 or c.index[-1] != ts:
        return False
    s50, s200 = float(c.iloc[-50:].mean()), float(c.iloc[-200:].mean())
    return float(c.iloc[-1]) > s50 > s200


def _red_on(red, ts):
    """ts 那天（或之前最近一天，最多差 5 天）總經風險燈是不是紅燈。"""
    if red is None:
        return False
    r = red.loc[:ts]
    return bool(len(r) and r.index[-1] >= ts - pd.Timedelta(days=5) and r.iloc[-1])


def step_cs(L, key, d, holdings, raw, bench, rule, slots, names, red=None):
    lev_sym, ref_sym = CS_LEV[key]
    ts = pd.Timestamp(d)
    ldf = raw.get(lev_sym)
    # 1. 前一個收盤日決定的槓桿 ETF 買賣，今天開盤成交
    order = L.get("lev_order")
    if order and ldf is not None and ts in ldf.index:
        opx = ldf.at[ts, "Open"] if "Open" in ldf.columns else None
        opx = float(opx) if opx is not None and pd.notna(opx) and opx > 0 else _px(ldf, ts)
        lev = L.get("lev")
        if opx and order == "sell" and lev:
            gross = lev["sh"] * opx
            fee = _fee(key, gross, "sell")
            L["cash"] += gross - fee
            pnl = gross - fee - lev["cost_total"]
            L["trades"].append({"date": d, "at": "open", "side": "賣", "t": lev_sym, "name": names.get(lev_sym, ""),
                                "sh": lev["sh"], "px": round(opx, 4), "amount": round(gross, 2), "fee": round(fee, 2),
                                "reason": L.get("lev_why") or "趨勢轉弱", "pnl": round(pnl, 2),
                                "pnl_pct": round(pnl / lev["cost_total"] * 100, 2), "buy_date": lev["date"],
                                "days": int(np.busday_count(_date.fromisoformat(lev["date"]), _date.fromisoformat(d)))})
            L["lev"] = None
        elif opx and order == "buy" and not lev:
            nav = L["cash"] + sum(p["sh"] * p["last"] for p in L["positions"].values())
            budget = min(nav * 0.5, L["cash"])
            sh = math.floor(budget / (opx * (1 + bt.COSTS.get(key, bt.COSTS["us"])["buy"])))
            while sh > 0 and sh * opx + _fee(key, sh * opx, "buy") > L["cash"]:
                sh -= 1
            if sh > 0:
                gross = sh * opx
                fee = _fee(key, gross, "buy")
                L["cash"] -= gross + fee
                L["lev"] = {"sh": sh, "cost": round((gross + fee) / sh, 4), "cost_total": round(gross + fee, 2),
                            "date": d, "last": opx}
                L["trades"].append({"date": d, "at": "open", "side": "買", "t": lev_sym, "name": names.get(lev_sym, ""),
                                    "sh": sh, "px": round(opx, 4), "amount": round(gross, 2), "fee": round(fee, 2),
                                    "reason": f"{ref_sym} 趨勢成立、總經不是紅燈"})
        L["lev_order"] = None
    # 2. 選股這一半照程式選股名單（每檔＝淨值 × 一半 ÷ 檔數），淨值含槓桿 ETF
    lev = L.get("lev")
    if lev:
        lev["last"] = _px(ldf, ts) or lev["last"]
    step(L, key, d, holdings, raw, bench, rule, slots, names, extra=lev["sh"] * lev["last"] if lev else 0.0, frac=0.5)
    # 3. 收盤判斷，下一個交易日開盤買賣槓桿 ETF
    trend, is_red = _trend_on(raw.get(ref_sym), ts), _red_on(red, ts)
    L["trend_on"], L["red"] = trend, is_red
    if trend and not is_red and not lev:
        L["lev_order"] = "buy"
    elif (not trend or is_red) and lev:
        L["lev_order"] = "sell"
        L["lev_why"] = "總經風險燈紅燈" if trend else f"{ref_sym} 趨勢轉弱"


def update_cs(cfg, raw_data, hist, data_dir, state_dir, now_iso, fetch):
    """fetch(代號清單) → {代號: 日線}，用來抓槓桿 ETF、原型和總經資料（scan.fetch_daily）。"""
    import pickle
    import time
    import macro
    key = cfg["key"]
    lev_sym, ref_sym = CS_LEV[key]
    path = state_dir / "sim_cs.json"
    try:
        L = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        L = {}
    if L.get("version") != VERSION:  # 規則改了：從頭重建
        L = _new_ledger(key)
        L.update({"book": "cs", "lev": None, "lev_order": None, "trend_on": None, "red": None})
    todo = [d for d in sorted(hist["confirmed"]) if d >= START_DATE and not (L["last"] and d <= L["last"])]
    cache = state_dir / "sim_cs_px.pkl"
    extra = None
    try:
        if not todo and time.time() - cache.stat().st_mtime < CS_REFRESH_MIN * 60:
            extra = pickle.loads(cache.read_bytes())
    except OSError:
        pass
    if extra is None:
        sfx = ".TW" if key == "tw" else ""
        got = fetch([lev_sym + sfx, ref_sym + sfx] + macro.SYMS)
        extra = {(s[: -len(sfx)] if sfx and s.endswith(sfx) else s): df for s, df in got.items()}
        cache.write_bytes(pickle.dumps(extra))
    ms = macro.series(extra)
    red = ms["total"] >= macro.RED if ms is not None else None
    rule = _exit_rule(cfg)
    # 槓桿 ETF 占一個位子，選股那一半放其餘幾檔；有核心設定的市場（台股）程式選股本來就是 4 檔＋核心，直接用 4 檔
    slots = cfg["target_count"] if cfg.get("core") else max(cfg["target_count"] - 1, 1)
    names = {**cfg["names"], **CS_NAMES}
    bench = raw_data.get(cfg["benchmark"])
    raw = {**{k: v for k, v in extra.items() if k in (lev_sym, ref_sym)}, **raw_data}
    for d in todo:
        if L["start_date"] is None:
            L["start_date"] = d
        step_cs(L, key, d, _top(hist["confirmed"][d], slots, cfg["themes"], cfg["max_per_theme"]), raw, bench, rule, slots,
                names, red)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(L, ensure_ascii=False), encoding="utf-8")
    view = dict(L, positions={**L["positions"], **({lev_sym: L["lev"]} if L.get("lev") else {})})
    out = summarize(view, key, raw, names, rule, slots, now_iso)
    out.update({"book": "cs", "title": "核心＋衛星",
                "lev": {"sym": lev_sym, "name": CS_NAMES[lev_sym], "ref": ref_sym, "trend_on": L.get("trend_on"),
                        "red": L.get("red"), "order": L.get("lev_order"), "held": bool(L.get("lev"))},
                "note": f"一半放 {lev_sym} {CS_NAMES[lev_sym]}：{ref_sym} 收盤站上 50 日線、50 日線在 200 日線上面，"
                        "而且總經風險燈不是紅燈才持有；收盤判斷、下一個交易日開盤買賣，不持有時放現金。"
                        "另一半放程式選股名單分數最高的幾檔，每檔＝淨值 × 一半 ÷ 檔數，出場規則和模擬倉一樣。"
                        "手續費、稅照模擬倉，股利不計。模擬帳戶，不是實際下單，不是投資建議。"})
    (data_dir / "sim").mkdir(parents=True, exist_ok=True)
    tmp = data_dir / "sim" / f"{key}_cs.json.tmp"
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(data_dir / "sim" / f"{key}_cs.json")
    return out
