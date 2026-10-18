"""模擬倉：照「程式選股」名單真的下單，記錄每筆買賣的時間、股數、價格、金額、手續費，算每天的淨值。

美股起始 50,000 美元、台股 1,000,000 台幣。每個收盤確認日用當天收盤價成交：
- 名單裡沒有了（被換掉）就賣；績效頁回測最好的出場規則觸發了（例如跌破 50 日線）也賣。
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

START_CASH = {"us": 50000.0, "tw": 1000000.0}
CURRENCY = {"us": "USD", "tw": "TWD"}
START_DATE = "2026-10-16"  # 第一個成交日（這天收盤後確認的名單）
VERSION = 1


def _fee(key, amount, side):
    c = bt.COSTS.get(key, bt.COSTS["us"])[side]
    f = amount * c
    return max(f, 20.0) if key == "tw" else f


def _best_exit(perf):
    """績效頁出場比較裡年化最高的規則（網頁標「建議」的那個）。"""
    rows = [r for r in (perf or {}).get("exits") or [] if r.get("cagr_pct") is not None]
    if not rows:
        return "none"
    return max(rows, key=lambda r: r["cagr_pct"])["rule"]


def _exit_hit(rule, df, d, pos):
    """d 那天收盤有沒有觸發出場。df 是未還原日線（Close/High/Low）。"""
    if rule in (None, "none") or df is None:
        return False
    s = df.loc[:d]
    if len(s) < 2:
        return False
    c = float(s["Close"].iloc[-1])
    if rule == "sma50":
        return len(s) >= 50 and c < float(s["Close"].iloc[-50:].mean())
    if rule == "pct8":
        return pos is not None and c < pos["cost"] * 0.92
    if rule == "atr3":
        if len(s) < 23:
            return False
        tr = pd.concat([s["High"] - s["Low"], (s["High"] - s["Close"].shift()).abs(),
                        (s["Low"] - s["Close"].shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.iloc[-22:].mean())
        return c < float(s["Close"].iloc[-22:].max()) - 3 * atr
    return False


def _px(df, d):
    if df is None or d not in df.index:
        return None
    v = df.at[d, "Close"]
    return float(v) if pd.notna(v) and v > 0 else None


def _new_ledger(key):
    return {"version": VERSION, "market": key, "currency": CURRENCY[key], "start_cash": START_CASH[key],
            "start_date": None, "cash": START_CASH[key], "positions": {}, "trades": [], "nav": [],
            "bench_units": None, "last": None}


def step(L, key, d, holdings, raw, bench, rule, slots, names):
    """處理一個收盤確認日 d（字串 YYYY-MM-DD）。"""
    ts = pd.Timestamp(d)
    pos = L["positions"]
    # 1. 賣：被換出名單，或觸發出場規則
    for t in list(pos):
        p = pos[t]
        df = raw.get(t)
        px = _px(df, ts)
        if px is None:
            continue
        reason = None
        if t not in holdings:
            reason = "換出名單"
        elif _exit_hit(rule, df, ts, p):
            reason = {"sma50": "跌破 50 日線", "pct8": "跌破成本 −8%", "atr3": "跌破吊燈停損"}.get(rule, "出場")
        if not reason:
            continue
        gross = p["sh"] * px
        fee = _fee(key, gross, "sell")
        net = gross - fee
        L["cash"] += net
        pnl = net - p["cost_total"]
        L["trades"].append({"date": d, "side": "賣", "t": t, "name": names.get(t, ""), "sh": p["sh"],
                            "px": round(px, 4), "amount": round(gross, 2), "fee": round(fee, 2), "reason": reason,
                            "pnl": round(pnl, 2), "pnl_pct": round(pnl / p["cost_total"] * 100, 2),
                            "buy_date": p["date"],
                            "days": int(np.busday_count(_date.fromisoformat(p["date"]), _date.fromisoformat(d)))})
        del pos[t]
    # 2. 買：名單裡有、手上沒有、沒觸發出場
    nav = L["cash"] + sum(p["sh"] * (_px(raw.get(t), ts) or p["last"]) for t, p in pos.items())
    slot = nav / max(slots, 1)
    for t in holdings:
        if t in pos:
            continue
        df = raw.get(t)
        px = _px(df, ts)
        if px is None or _exit_hit(rule, df, ts, None):
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
                            "站回出場線後買回" if any(x["t"] == t and x["side"] == "賣" and x["reason"] != "換出名單"
                                                    for x in L["trades"][-100:]) else "選入名單"})
    # 3. 收盤淨值
    for t, p in pos.items():
        p["last"] = _px(raw.get(t), ts) or p["last"]
    nav = L["cash"] + sum(p["sh"] * p["last"] for p in pos.values())
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
    return {
        "market": key, "currency": L["currency"], "generated_at": now_iso, "start_date": L["start_date"],
        "start_cash": start, "data_date": today, "exit_rule": rule, "slots": slots,
        "nav": last_nav, "live_nav": round(live, 2), "cash": round(L["cash"], 2),
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
                "台股手續費最低 20 元，股利不計。出場規則跟績效頁回測最好的那個。模擬帳戶，不是實際下單，不是投資建議。",
    }


def update(cfg, raw_data, hist, data_dir, state_dir, now_iso):
    key = cfg["key"]
    path = state_dir / "sim.json"
    try:
        L = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        L = _new_ledger(key)
    try:
        perf = json.loads((data_dir / "perf" / f"{key}.json").read_text(encoding="utf-8"))
    except Exception:
        perf = {}
    rule = _best_exit(perf)
    slots = cfg["target_count"]
    names = cfg["names"]
    bench = raw_data.get(cfg["benchmark"])
    raw = {t: df for t, df in raw_data.items()}
    for d in sorted(hist["confirmed"]):
        if d < START_DATE or (L["last"] and d <= L["last"]):
            continue
        if L["start_date"] is None:
            L["start_date"] = d
        step(L, key, d, list(hist["confirmed"][d]["holdings"]), raw, bench, rule, slots, names)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(L, ensure_ascii=False), encoding="utf-8")
    out = summarize(L, key, raw, names, rule, slots, now_iso)
    (data_dir / "sim").mkdir(parents=True, exist_ok=True)
    tmp = data_dir / "sim" / f"{key}.json.tmp"
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(data_dir / "sim" / f"{key}.json")
    return out
