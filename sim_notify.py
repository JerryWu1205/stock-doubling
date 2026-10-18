"""模擬倉 Telegram 通知：每筆模擬買賣都發（同一天的交易合成一則），發過的不再發。

run.bat 在 notify.py 之後呼叫。讀 docs/data/sim/<市場>.json 的交易紀錄，
已通知的交易記在 state/<市場>/sim_notify.json。Bot 設定沿用 notify.py（AI情報官的 Bot）。

用法：
  python sim_notify.py            正常執行
  python sim_notify.py --dry-run  只印出要發的訊息，不發送、不寫狀態
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import notify

HERE = notify.HERE
LABEL = {"us": "美股", "tw": "台股"}


def money(v, cur, d=None):
    d = (0 if cur == "TWD" else 2) if d is None else d
    return ("NT$" if cur == "TWD" else "US$") + f"{v:,.{d}f}"


def signed(v, cur, d=None):
    return ("+" if v > 0 else "−" if v < 0 else "") + money(abs(v), cur, d)


def pct(v):
    return "–" if v is None else f"{v:+.2f}%"


def trade_key(x):
    return f"{x['date']}|{x['side']}|{x['t']}"


def trade_line(x, cur):
    nm = f"{x['t']} {x['name']}" if x.get("name") and x["name"] != x["t"] else x["t"]
    s = f"{'🟢買' if x['side'] == '買' else '🔴賣'} {nm} {x['sh']:,} 股 @ {x['px']:,.2f} ＝ {money(x['amount'], cur)}（費用 {money(x['fee'], cur)}）"
    if x["side"] == "賣":
        s += f"\n    損益 {signed(x['pnl'], cur)}（{pct(x['pnl_pct'])}）・持有 {x.get('days', '?')} 天"
    if x.get("reason"):
        s += f"｜{x['reason']}"
    return s


def build(key, sim, sent):
    cur = sim["currency"]
    new = [x for x in reversed(sim.get("trades") or []) if trade_key(x) not in sent]  # 舊到新
    msgs, keys = [], []
    for d in sorted({x["date"] for x in new}):
        xs = [x for x in new if x["date"] == d]
        xs.sort(key=lambda x: (x["side"] != "賣", x["t"]))  # 先賣後買
        r, b, g = sim.get("ret") or {}, sim.get("bench") or {}, sim.get("goal") or {}
        head = f"🧪 模擬倉｜{LABEL.get(key, key)} {d} 收盤成交 {len(xs)} 筆"
        foot = (f"\n淨值 {money(sim['nav'], cur, 0)}（總報酬 {pct(r.get('total'))}，今日 {pct(r.get('day'))}）"
                f"\n同樣的錢買大盤 {pct(b.get('total'))}・翻倍目標 {pct(g.get('total'))}"
                f"\n現金 {money(sim['cash'], cur, 0)}・持股 {len(sim.get('positions') or [])} 檔"
                "\n（模擬帳戶，不是實際下單，不是投資建議）")
        msgs.append(head + "\n\n" + "\n".join(trade_line(x, cur) for x in xs) + "\n" + foot)
        keys += [trade_key(x) for x in xs]
    return msgs, keys


def main():
    ap = argparse.ArgumentParser(description="模擬倉 Telegram 通知")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    settings = notify.load_settings()
    if not settings["enabled"]:
        return 0
    token = chat_id = None
    rc = 0
    for key in ("us", "tw"):
        path = notify.DATA_DIR / "sim" / f"{key}.json"
        if not path.exists():
            continue
        spath = HERE / "state" / key / "sim_notify.json"
        try:
            sim = json.loads(path.read_text(encoding="utf-8"))
            try:
                sent = set(json.loads(spath.read_text(encoding="utf-8"))["sent"])
            except (OSError, ValueError, KeyError):
                sent = set()
            msgs, keys = build(key, sim, sent)
            if args.dry_run:
                for m in msgs:
                    print(m, end="\n\n")
                print(f"（dry-run {key}：{len(msgs)} 則，未發送）")
                continue
            if not msgs:
                continue
            if token is None:
                env = notify.read_env(settings["env_file"])
                token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
            if not token or not chat_id:
                raise RuntimeError(f"{settings['env_file']} 沒有 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
            for m in msgs:  # 發送失敗就不寫狀態，下次重發
                notify.send(m, token, chat_id)
            sent |= set(keys)
            keep = sorted(sent)[-2000:]
            spath.parent.mkdir(parents=True, exist_ok=True)
            tmp = spath.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sent": keep}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(spath)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            print(f"[{key}] {now} 模擬倉已發送 {len(msgs)} 則交易通知")
        except Exception as e:
            print(f"[{key}] 模擬倉通知失敗：{notify._TOKEN_RE.sub('bot<TOKEN>', str(e))}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
