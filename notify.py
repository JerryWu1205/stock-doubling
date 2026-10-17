"""股票翻倍計畫：Telegram 通知（經由 AI情報官 的 Bot）。

run.bat 每次在 scan.py 之後呼叫。美股、台股各自讀 docs/data/<市場>.json，
跟上一次通知過的狀態比對，有變化才發訊息；同一件事只發一次。
狀態存在 state/<市場>/notify_state.json（不進 git）。

  盤中暫定：持股出現出場訊號 → 立即通知（每檔每天一次）
  收盤確認：自動換股、持股訊號改變、換股倒數、自選持股落後 → 一則彙整（每個交易日一次，沒變化不發）

用法：
  python notify.py            正常執行
  python notify.py --dry-run  只印出要發的訊息，不發送、不寫狀態
"""

import argparse
import json
import re
import sys
import tomllib
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "docs" / "data"
KEEP_DAYS = 10
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

FLAGS = {"us": "🇺🇸", "tw": "🇹🇼"}
LABELS = {"strong": "強勢續抱", "hold": "續抱", "reduce": "轉弱留意", "exit": "出場訊號"}
RANK = {"strong": 3, "hold": 2, "reduce": 1, "exit": 0}


def base_signal(h):
    """跟 scan.holding_signal 同一套規則；data 裡的 signal 可能被改成 swap_out，這裡要原始訊號。"""
    if h["score"] < 40 and not h["above_sma50"]:
        return "exit"
    if h["score"] < 50:
        return "reduce"
    if h["score"] >= 75:
        return "strong"
    return "hold"


# ───────── 設定與狀態 ─────────

def load_settings():
    with open(HERE / "config.toml", "rb") as f:
        n = tomllib.load(f).get("notify", {})
    return {
        "enabled": n.get("enabled", True),
        "env_file": (HERE / n.get("env_file", "../ai-intel-officer/.env")).resolve(),
        "page_url": n.get("page_url", ""),
    }


def read_env(path):
    env = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def state_path(key):
    return HERE / "state" / key / "notify_state.json"


def load_state(key):
    try:
        with open(state_path(key), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(key, state, today):
    cutoff = date.fromisoformat(today).toordinal() - KEEP_DAYS
    state["sent"] = {k: d for k, d in state["sent"].items()
                     if k.startswith("manual_lag:") or date.fromisoformat(d).toordinal() >= cutoff}
    path = state_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


# ───────── Telegram ─────────

def send(text, token, chat_id):
    for i in range(0, len(text), 4000):
        body = json.dumps({"chat_id": chat_id, "text": text[i:i + 4000],
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.load(r)
        if not res.get("ok"):
            raise RuntimeError(f"Telegram 發送失敗：{res.get('description')}")


# ───────── 訊息內容 ─────────

def title(data):
    return f"{FLAGS.get(data.get('market'), '')}{data.get('market_label', '美股')}"


def name(h):
    return f"{h['ticker']} {h['name']}" if h.get("name") else h["ticker"]


def fmt_reasons(h):
    return "、".join(h.get("reasons") or []) or "—"


def manual_lags(data):
    """自選持股（程式不會自動換）的確認換股訊號：{out: [swap, ...]}。"""
    manual = {h["ticker"] for h in data["holdings"] if h.get("source") == "manual"}
    out = {}
    for s in data.get("swaps", []):
        if s["status"] == "confirmed" and s["out"] in manual:
            out.setdefault(s["out"], []).append(s)
    return out


def lag_line(data, t, swaps):
    best = max(swaps, key=lambda s: s["gap"] or 0)
    streak = max(s["streak"] for s in swaps)
    h = next((h for h in data["holdings"] if h["ticker"] == t), {"ticker": t})
    return (f"⚠️ {name(h)}（你自選的）已連續 {streak} 天落後 {len(swaps)} 檔候選股，"
            f"差最多的是 {best['in']}（{best['gap']:.0f} 分）。程式不會自動換，要不要換由你決定。")


def countdown_lines(data):
    auto = {h["ticker"] for h in data["holdings"] if h.get("source") == "auto"}
    rows = []
    for c in data.get("candidates", []):
        s = c.get("swap")
        if s and s["status"] == "building" and s["out"] in auto and s["streak"] == s["need"] - 1:
            rows.append((s["gap"] or 0, f"⏳ {name(c)} 已連續 {s['streak']}/{s['need']} 天比 "
                                        f"{s['out']} 高 {s['gap']:.0f} 分，下個收盤再成立就會自動換股"))
    return [r for _, r in sorted(rows, reverse=True)[:3]]


def score_line(hs):
    return "、".join(f"{name(h)} {h['score']:.0f}" for h in hs)


def snapshot(data, settings):
    hs = data["holdings"]
    lines = [f"✅ 股票翻倍計畫{title(data)}已接上 AI情報官（{data['mode_label']}，資料日 {data['data_date']}）",
             "之後有換股、訊號改變、盤中出場訊號時會在這裡通知。", "",
             f"持股 {len(hs)} 檔",
             f"💪 最強：{score_line(hs[:3])}",
             f"🥶 最弱：{score_line(hs[-3:])}"]
    exits = [h for h in hs if base_signal(h) == "exit"]
    if exits:
        lines.append(f"🚨 出場訊號：{score_line(exits)}")
    for t, swaps in manual_lags(data).items():
        lines.append(lag_line(data, t, swaps))
    lines += countdown_lines(data)
    if settings["page_url"]:
        lines += ["", f"🔗 {settings['page_url']}"]
    return "\n".join(lines)


def intraday_events(data, state):
    msgs = []
    for h in data["holdings"]:
        t = h["ticker"]
        key = f"intraday_exit:{data['data_date']}:{t}"
        if base_signal(h) != "exit" or key in state["sent"] or state["signals"].get(t) == "exit":
            continue
        price = f"現價 {h['price']}，" if h.get("price") is not None else ""
        msgs.append((key, f"🚨 翻倍計畫{title(data)}盤中警報：{name(h)}\n"
                          f"{price}今天 {h['chg_1d_pct']:+.2f}%，分數 {h['score']:.1f}，"
                          f"{'仍在' if h['above_sma50'] else '已跌破'} 50 日線，盤中出現出場訊號。\n"
                          f"原因：{fmt_reasons(h)}\n"
                          f"（盤中暫定，收盤後才確認）"))
    return msgs


def change_lines(data, state):
    """當天的自動換股、選入，每筆只通知一次（收盤後重跑或手動補跑時也不會漏或重複）。"""
    lines, keys = [], []
    for c in data.get("portfolio_changes", []):
        if c["date"] != data["data_date"]:
            continue
        if c["action"] == "swap":
            k = f"swap:{c['date']}:{c['out']}:{c['in']}"
            text = f"🔄 自動換股：賣 {c['out']} → 買 {c['in']}（差 {c['gap']:.0f} 分，連續 {c['streak']} 天）"
        elif c["action"] == "add":
            k = f"add:{c['date']}:{c['ticker']}"
            text = f"➕ 自動選入：{c['ticker']}（{c['score']:.0f} 分）"
        else:
            continue
        if k not in state["sent"]:
            lines.append(text)
            keys.append(k)
    return lines, keys


def close_events(data, state):
    """回傳 (彙整訊息或 None, 要記錄的 keys)。"""
    d = data["data_date"]
    lines, keys = change_lines(data, state)
    for h in data["holdings"]:
        t, new = h["ticker"], base_signal(h)
        old = state["signals"].get(t)
        if old and old != new:
            arrow = "⬆️" if RANK[new] > RANK[old] else "⬇️"
            icon = "🚨 " if new == "exit" else ""
            lines.append(f"{icon}{arrow} {name(h)}：{LABELS[old]} → {LABELS[new]}（{h['score']:.0f} 分，{fmt_reasons(h)}）")
    lags = manual_lags(data)
    for t, swaps in lags.items():
        k = f"manual_lag:{t}"
        if k not in state["sent"]:
            lines.append(lag_line(data, t, swaps))
            keys.append(k)
    for k in [k for k in state["sent"] if k.startswith("manual_lag:")]:
        if k.split(":", 1)[1] not in lags:  # 已經不落後了，下次再落後要重新通知
            del state["sent"][k]
    lines += countdown_lines(data)
    if not lines:
        return None, keys
    return f"📊 翻倍計畫{title(data)}收盤確認（{d}）\n\n" + "\n".join(lines), keys


def plan(data, state, settings):
    """回傳 (要發的訊息清單, 更新後的 state)。"""
    today = data["data_date"]
    msgs = []
    if state is not None and not state.get("changes_tracked"):
        # 舊版狀態檔沒記換股紀錄：已經存在的異動當作通知過，避免一次補發一堆
        for k in change_lines(data, state)[1]:
            state["sent"][k] = today
        state["changes_tracked"] = True
    if state is None:
        state = {"signals": {}, "sent": {}, "close_done": None, "changes_tracked": True}
        msgs.append(snapshot(data, settings))
        for t in manual_lags(data):
            state["sent"][f"manual_lag:{t}"] = today
        for k in change_lines(data, state)[1]:
            state["sent"][k] = today
        if data["mode"] == "close":
            state["close_done"] = today
    elif data["mode"] == "intraday":
        for key, text in intraday_events(data, state):
            msgs.append(text)
            state["sent"][key] = today
        return msgs, state
    elif state.get("close_done") != today:
        text, keys = close_events(data, state)
        if text:
            msgs.append(text)
        for k in keys:
            state["sent"][k] = today
        state["close_done"] = today
    else:
        # 今天的彙整已發過；之後才發生的換股（例如手動補跑收盤）另外通知
        lines, keys = change_lines(data, state)
        if lines:
            msgs.append(f"📊 翻倍計畫{title(data)}換股（{today}）\n\n" + "\n".join(lines))
        for k in keys:
            state["sent"][k] = today
        if not lines:
            return msgs, state
    # 收盤確認（或第一次執行）才更新基準訊號
    if data["mode"] == "close" or not state["signals"]:
        state["signals"] = {h["ticker"]: base_signal(h) for h in data["holdings"]}
    return msgs, state


def market_files():
    try:
        with open(DATA_DIR / "markets.json", encoding="utf-8") as f:
            keys = [m["key"] for m in json.load(f)["markets"]]
    except (OSError, json.JSONDecodeError, KeyError):
        keys = ["us", "tw"]
    return [(k, DATA_DIR / f"{k}.json") for k in keys if (DATA_DIR / f"{k}.json").exists()]


def main():
    ap = argparse.ArgumentParser(description="股票翻倍計畫 Telegram 通知")
    ap.add_argument("--dry-run", action="store_true", help="只印出訊息，不發送、不寫狀態")
    args = ap.parse_args()

    settings = load_settings()
    if not settings["enabled"]:
        return 0
    token = chat_id = None
    rc = 0
    for key, path in market_files():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("demo") or not data.get("holdings"):
                continue
            msgs, state = plan(data, load_state(key), settings)
            if args.dry_run:
                for m in msgs:
                    print(m, end="\n\n")
                print(f"（dry-run {key}：{len(msgs)} 則，未發送）")
                continue
            if msgs:
                if token is None:
                    env = read_env(settings["env_file"])
                    token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
                if not token or not chat_id:
                    raise RuntimeError(f"{settings['env_file']} 沒有 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
                for m in msgs:  # 發送失敗就不寫狀態，下次重發
                    send(m, token, chat_id)
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                print(f"[{key}] {now} 已發送 {len(msgs)} 則通知到 Telegram")
            save_state(key, state, data["data_date"])
        except Exception as e:
            print(f"[{key}] 通知失敗：{_TOKEN_RE.sub('bot<TOKEN>', str(e))}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
