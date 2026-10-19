"""我的真實持股：每天台股、美股收盤確認後，用 Telegram 發一則建議報告。

持股寫在 my_holdings.toml（只在這台電腦，不進 git、不上公開網站），格式：
  [us]
  MRVL = { shares = 30, avg = 235 }
  [tw]
  2330 = { shares = 1000, avg = 1000 }

run.bat 在 sim_notify.py 之後呼叫。只在 docs/data/<市場>.json 是「收盤確認」時發，
每個市場每個資料日只發一次（記在 state/<市場>/holdings_report.json，也記每檔連續幾天在停損線下）。
賣出建議不只看跌幅：先判斷是大盤／族群一起跌、還是個股獨弱或有利空（docs/data/news.json）。
個股的問題照停損執行；大盤一起跌就先賣一半或先觀察，隔天收盤還在線下才做完。
Bot 設定沿用 notify.py（AI情報官的 Bot）。

用法：
  python holdings_report.py            正常執行
  python holdings_report.py --dry-run  只印出要發的訊息，不發送、不寫狀態
"""

import argparse
import json
import sys
import tomllib
from datetime import date, datetime, timezone

import notify

HERE = notify.HERE
DATA = notify.DATA_DIR
LABEL = {"us": "美股", "tw": "台股"}
STOP_PCT = 0.08   # 成本 −8% 停損
EARN_DAYS = 5     # 財報前幾天提醒


def load_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_holdings():
    p = HERE / "my_holdings.toml"
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    return {m: {str(t).upper(): v for t, v in (raw.get(m) or {}).items()} for m in ("us", "tw")}


def lookup(t, data, allm):
    """先找程式選股、候選、跌深名單（有分數和退場價），找不到再用全部股票的收盤價。"""
    for i in data.get("holdings") or []:
        if i["ticker"] == t:
            return i, "程式選股持股"
    for i in data.get("candidates") or []:
        if i["ticker"] == t:
            return i, "候選"
    for b in (data.get("books") or {}).values():
        for i in b.get("candidates") or []:
            if i["ticker"] == t:
                return i, "跌深名單"
    if allm:
        cols = allm["cols"]
        for r in allm["rows"]:
            if str(r[0]).upper() == t:
                x = dict(zip(cols, r))
                return {"ticker": t, "name": x.get("name") or "", "price": x.get("px"),
                        "chg_1d_pct": x.get("chg_pct"),
                        "exit": {"sma50": x.get("sma50"), "hi22": x.get("hi22")}}, "不在名單"
    return None, "找不到報價"


def fmt(v, cur):
    return "–" if v is None else (f"{v:,.0f}" if cur == "TWD" and v >= 1000 else f"{v:,.2f}")


def money(v, cur):
    s = "−" if v < 0 else "+" if v > 0 else ""
    return s + ("NT$" if cur == "TWD" else "US$") + f"{abs(v):,.0f}"


def chg_n(spark, n):
    return (spark[-1] / spark[-1 - n] - 1) * 100 if spark and len(spark) > n and spark[-1 - n] else None


def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def context(i, data, news, vix):
    """判斷這次下跌是大盤／族群一起跌，還是個股自己弱或有利空。回傳 (類型, 說明行)。
    類型：news（個股利空）、alone（個股獨弱）、market（大盤一起跌）、sector（族群走弱）、none。"""
    t, theme = i["ticker"], i.get("theme")
    mkt = (data.get("benchmark_quote") or {}).get("chg_1d_pct")
    peers = [c for c in (data.get("candidates") or []) if c.get("theme") == theme and c["ticker"] != t] if theme else []
    s1, s5 = i.get("chg_1d_pct"), chg_n(i.get("spark"), 5)
    p1, p5 = avg(c.get("chg_1d_pct") for c in peers), avg(chg_n(c.get("spark"), 5) for c in peers)
    parts = [f"大盤 {mkt:+.1f}%" if mkt is not None else None,
             f"{theme}族群 {p1:+.1f}%（5 天 {p5:+.1f}%）" if p1 is not None and p5 is not None else None,
             f"本檔 {s1:+.1f}%" + (f"（5 天 {s5:+.1f}%）" if s5 is not None else "") if s1 is not None else None,
             f"VIX {vix:.1f}" if vix else None]
    lines = ["今日盤勢：" + "、".join(x for x in parts if x)]
    keys = {t, i.get("name") or t}
    bad, hits = False, []
    for n in (news or [])[:40]:
        tk = " ".join(n.get("tickers") or [])
        if not any(k and k in tk for k in keys):
            continue
        imp = n.get("impact") or ""
        neg = "空" in imp and any(k and k in imp.split("空", 1)[1].split("多", 1)[0] for k in keys | {theme or "~"})
        bad |= neg
        hits.append(f"{'📉利空' if neg else '📰'} {n.get('time', '')[5:10]} {n['title'][:60]}")
        if len(hits) >= 2:
            break
    lines += hits
    rel1 = None if s1 is None or p1 is None else s1 - p1
    rel5 = None if s5 is None or p5 is None else s5 - p5
    if bad:
        kind = "news"
    elif (rel5 is not None and rel5 <= -5) or (rel1 is not None and rel1 <= -3):
        kind = "alone"
    elif mkt is not None and mkt <= -1 and p1 is not None and p1 <= -1.5:
        kind = "market"
    elif p5 is not None and p5 <= -5:
        kind = "sector"
    else:
        kind = "none"
    lines.append("判斷：" + {"news": "有個股利空消息", "alone": "個股比族群弱很多（自己的問題）",
                             "market": "大盤和族群一起跌（整體情緒，不是個股問題）",
                             "sector": "整個族群資金在撤（輪動走弱）", "none": "沒有明顯整體下跌或利空"}[kind])
    return kind, lines


def advise(i, pos, cur, earn, today, ctx, days):
    """回傳 (圖示, 動作, 細節行)。價格規則再加上盤勢判斷：
    個股利空或獨弱 → 照規則執行；大盤一起跌 → 先賣一半／先觀察，隔天收盤還在線下才做完。"""
    px, ex = i.get("price"), i.get("exit") or {}
    sh, avgp = float(pos["shares"]), float(pos["avg"])
    stop, trail, sma50 = avgp * (1 - STOP_PCT), ex.get("trail"), ex.get("sma50")
    lines = []
    if px is None:
        return "⚪", "沒有報價", lines
    pnl = (px - avgp) * sh
    lines.append(f"收盤 {fmt(px, cur)}（今日 {i.get('chg_1d_pct') or 0:+.1f}%）・成本 {fmt(avgp, cur)}・"
                 f"{sh:,.0f} 股・損益 {money(pnl, cur)}（{(px / avgp - 1) * 100:+.1f}%）")
    lv = [f"50日線 {fmt(sma50, cur)}"] if sma50 else []
    if trail:
        lv.append(f"吊燈停損 {fmt(trail, cur)}")
    lv.append(f"成本−8% {fmt(stop, cur)}")
    lines.append("退場線：" + "、".join(lv))
    if i.get("score") is not None:
        chg = i.get("score_chg")
        lines.append(f"分數 {i['score']:.0f}" + (f"（{chg:+.0f}）" if chg else ""))
    kind, cl = ctx
    lines += cl
    half = f"{int(sh // 2):,} 股"
    why = {"news": "，有個股利空", "alone": "，個股比族群弱", "sector": "，族群資金在撤出"}.get(kind, "")
    systemic = kind in ("market", "none")
    if px < stop:
        if not systemic or days >= 2:
            icon, act = "🔴", ("跌破成本 −8%" + (why if not systemic else f"，已連續 {days} 天沒站回")
                              + "，建議全部賣出")
        else:
            icon, act = "🟠", f"跌破成本 −8%，但看起來是大盤一起跌：先賣一半（{half}），明天收盤還沒站回 {fmt(stop, cur)} 再賣剩下的"
    elif trail and px < trail:
        if not systemic or days >= 2:
            icon, act = "🟠", "跌破吊燈停損" + (why if not systemic else f"，已連續 {days} 天") + f"，建議先賣一半（{half}）"
        else:
            icon, act = "🟡", f"跌破吊燈停損，但看起來是大盤一起跌：今天先不賣，明天收盤還在 {fmt(trail, cur)} 下面就賣一半"
    elif sma50 and px < sma50:
        icon, act = "🟡", f"在 50 日線下，續抱但不加碼；站回 {fmt(sma50, cur)} 才算轉強"
        if kind in ("news", "alone"):
            act += "；個股偏弱，可先減碼"
    else:
        icon, act = "🟢", "在退場線之上，續抱"
        if kind == "news":
            act += "；有利空消息，留意明天走勢"
    if i.get("signal") == "take":
        act += "；反彈接近半年高點，可分批獲利"
    if earn:
        d = (date.fromisoformat(earn["date"]) - today).days
        if 0 <= d <= EARN_DAYS:
            when = earn.get("tw_time") or earn["date"]
            lines.append(f"⚠️ {d} 天後財報（台灣時間 {when}{'，預估日' if earn.get('est') else ''}）：不加碼，"
                         f"沒有獲利緩衝就考慮財報前減碼")
        elif d > EARN_DAYS:
            lines.append(f"下次財報 {earn['date']}{'（預估）' if earn.get('est') else ''}")
    return icon, act, lines


def build(key, holds, breach):
    data = load_json(DATA / f"{key}.json")
    if not data or data.get("mode") != "close" or not holds:
        return None, None, None
    cur, today = data.get("currency", "USD"), date.fromisoformat(data["data_date"])
    allm = load_json(DATA / "all" / f"{key}.json")
    fund = (load_json(DATA / "fund" / f"{key}.json") or {}).get("items") or {}
    news = (load_json(DATA / "news.json") or {}).get("news") or []
    dash = (load_json(DATA / "dash.json") or {}).get("items", [])
    vix = next((x.get("value") for x in dash if x.get("sym") == "^VIX"), None)
    blocks, total, cost, newb = [], 0.0, 0.0, {}
    for t, pos in holds.items():
        i, src = lookup(t, data, allm)
        nm = f"{t} {i['name']}" if i and i.get("name") and i["name"] != t else t
        if not i:
            blocks.append(f"⚪ {nm}：{src}")
            continue
        earn = (fund.get(t) or {}).get("earnings")
        ex, px = i.get("exit") or {}, i.get("price")
        under = px is not None and (px < float(pos["avg"]) * (1 - STOP_PCT) or (ex.get("trail") and px < ex["trail"]))
        last = breach.get(t) or {}
        n = (last.get("n", 0) + (last.get("date") != data["data_date"])) if under else 0
        if under:
            newb[t] = {"date": data["data_date"], "n": max(n, 1)}
        icon, act, lines = advise(i, pos, cur, earn, today, context(i, data, news, vix), max(n, 1) if under else 0)
        blocks.append(f"{icon} {nm}（{src}）\n👉 {act}\n" + "\n".join("    " + x for x in lines))
        if i.get("price") is not None:
            total += i["price"] * float(pos["shares"])
            cost += float(pos["avg"]) * float(pos["shares"])
    head = f"📋 我的持股｜{LABEL[key]} {data['data_date']} 收盤"
    if cost:
        head += f"\n市值 {money(total, cur).lstrip('+')}・未實現損益 {money(total - cost, cur)}（{(total / cost - 1) * 100:+.1f}%）"
    macro = next((x for x in dash if x.get("kind") == "macro"), None)
    if macro:
        light = {"red": "🔴 紅燈", "yellow": "🟡 黃燈", "green": "🟢 綠燈"}.get(macro.get("light"), macro.get("light"))
        head += f"\n總經風險燈 {light}（{macro.get('value')} 分）" + ("：先不要加碼" if macro.get("light") == "red" else "")
    msg = head + "\n\n" + "\n\n".join(blocks) + "\n（依程式退場規則產生，不是投資建議）"
    return msg, data["data_date"], newb


def main():
    ap = argparse.ArgumentParser(description="我的持股收盤建議報告")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    settings = notify.load_settings()
    if not settings["enabled"]:
        return 0
    try:
        hold = load_holdings()
    except (OSError, ValueError) as e:
        print(f"my_holdings.toml 讀取失敗：{e}", file=sys.stderr)
        return 1
    rc = 0
    for key in ("us", "tw"):
        spath = HERE / "state" / key / "holdings_report.json"
        try:
            st = load_json(spath) or {}
            last = st.get("date")
            msg, d, newb = build(key, hold.get(key) or {}, st.get("breach") or {})
            if not msg:
                continue
            if args.dry_run:
                print(msg, f"\n（dry-run {key}，上次發送 {last}，未發送）\n")
                continue
            if last == d:
                continue
            env = notify.read_env(settings["env_file"])
            token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                raise RuntimeError(f"{settings['env_file']} 沒有 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
            notify.send(msg, token, chat_id)  # 發送失敗就不寫狀態，下次重發
            spath.parent.mkdir(parents=True, exist_ok=True)
            tmp = spath.with_suffix(".tmp")
            tmp.write_text(json.dumps({"date": d, "breach": newb}), encoding="utf-8")
            tmp.replace(spath)
            print(f"[{key}] {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} 持股報告已發送（{d}）")
        except Exception as e:
            print(f"[{key}] 持股報告失敗：{notify._TOKEN_RE.sub('bot<TOKEN>', str(e))}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
