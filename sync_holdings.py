"""my_holdings.toml 和網頁「我的」分頁雙向同步（透過網頁的 ☁ 跨裝置同步，加密的私人 gist）。

同步碼放在 sync.env（只在這台電腦，*.env 不進 git）：把網頁「連結其他裝置」的整段連結貼進去就好，
  https://jerrywu1205.github.io/stock-doubling/#sync=ghp_xxx.gistid.key

規則（run.bat 每次 holdings_report.py 之前呼叫）：
  - my_holdings.toml 內容變了（例如 Claude 照 Firstrade 截圖改的）→ 加密寫回 gist，手機、電腦網頁下次同步就更新。
    toml 裡有的檔數和均價蓋掉網頁的；網頁有股數、toml 沒有的視為已賣掉，從網頁移除；只按 ☆ 沒填股數的不動。
  - 網頁那邊的股數均價變了（例如在手機按「賣出」）→ 改寫 my_holdings.toml，每日持股報告也跟著對。
  - 兩邊都變了，以 my_holdings.toml 為準。
加密和網頁一樣：AES-256-GCM，iv 12 bytes，標準 base64；鑰匙是 base64url。
狀態記在 state/holdings_sync.json（只有時間戳和雜湊，不含持股；也不進 git）。

用法：
  python sync_holdings.py            正常執行
  python sync_holdings.py --dry-run  只印出要做什麼，不寫 gist、不改檔
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = HERE / "sync.env"
TOML = HERE / "my_holdings.toml"
STATE = HERE / "state" / "holdings_sync.json"
SYNC_FILE = "stock-doubling-sync.json"
MARKETS = ("us", "tw")
CODE_RE = re.compile(r"((?:ghp_|github_pat_|gho_)\w+)\.([0-9a-f]+)\.([\w-]{40,})")


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_cfg():
    if not ENV.exists():
        return None
    m = CODE_RE.search(ENV.read_text(encoding="utf-8"))
    return {"token": m[1], "gist": m[2], "key": b64u_dec(m[3])} if m else None


def gh(cfg, method="GET", body=None):
    req = urllib.request.Request(
        "https://api.github.com/gists/" + cfg["gist"], method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + cfg["token"], "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "stock-doubling-sync"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def read_remote(cfg):
    g = gh(cfg)
    f = (g.get("files") or {}).get(SYNC_FILE)
    if not f:
        raise RuntimeError("gist 裡找不到同步資料")
    text = f["content"]
    if f.get("truncated"):
        with urllib.request.urlopen(f["raw_url"], timeout=30) as r:
            text = r.read().decode()
    return decrypt(cfg["key"], json.loads(text or "{}"))


def decrypt(key, box):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not box.get("ct"):
        return {"keys": {}}
    pt = AESGCM(key).decrypt(base64.b64decode(box["iv"]), base64.b64decode(box["ct"]), None)
    return json.loads(pt.decode())


def encrypt(key, obj):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), None)
    return {"v": 1, "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()}


def write_remote(cfg, keys):
    box = encrypt(cfg["key"], {"v": 1, "at": int(time.time() * 1000), "keys": keys})
    gh(cfg, "PATCH", {"files": {SYNC_FILE: {"content": json.dumps(box)}}})


def load_toml():
    """{市場: {代號: {"sh": 股數, "avg": 均價}}}，只收 toml 裡有的市場段落。"""
    if not TOML.exists():
        return {}
    with open(TOML, "rb") as f:
        raw = tomllib.load(f)
    out = {}
    for m in MARKETS:
        if m in raw:
            out[m] = {str(t).upper(): {"sh": float(v["shares"]), "avg": float(v["avg"])}
                      for t, v in (raw[m] or {}).items() if v.get("shares") and v.get("avg")}
    return out


def num(x):
    return f"{x:.6f}".rstrip("0").rstrip(".")


def dump_toml(h):
    lines = ["# 我的真實持股（只在這台電腦，不進 git）。sync_holdings.py 會和網頁「我的」分頁雙向同步。"]
    for m in MARKETS:
        if m not in h:
            continue
        lines += ["", f"[{m}]"]
        for t in sorted(h[m]):
            p = h[m][t]
            lines.append(f'"{t}" = {{ shares = {num(p["sh"])}, avg = {num(p["avg"])} }}')
    return "\n".join(lines) + "\n"


def canon(h):
    return hashlib.sha256(json.dumps(h, sort_keys=True).encode()).hexdigest()


def jload(s, default):
    try:
        v = json.loads(s)
        return v if isinstance(v, type(default)) else default
    except (TypeError, ValueError):
        return default


def from_remote(keys):
    out = {}
    for m in MARKETS:
        k = keys.get("pos_" + m)
        if k is None:
            continue
        pos = jload(k.get("v"), {})
        out[m] = {str(t).upper(): {"sh": float(p["sh"]), "avg": float(p["avg"])}
                  for t, p in pos.items() if isinstance(p, dict) and (p.get("sh") or 0) > 0 and (p.get("avg") or 0) > 0}
    return out


def apply_toml(keys, h, now):
    """把 toml 的持股寫進網頁的 pos_<市場>、mine_<市場>；回傳有沒有改。"""
    changed = False
    for m, want in h.items():
        old = jload((keys.get("pos_" + m) or {}).get("v"), {})
        new = {t: {**(old.get(t) if isinstance(old.get(t), dict) else {}), "sh": p["sh"], "avg": p["avg"]} for t, p in want.items()}
        gone = [t for t in old if t not in new]
        mine = jload((keys.get("mine_" + m) or {}).get("v"), [])
        mine2 = [t for t in mine if t not in gone] + [t for t in sorted(new) if t not in mine]
        for k, v in (("pos_" + m, new), ("mine_" + m, mine2)):
            s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            if (keys.get(k) or {}).get("v") is None or jload(keys[k]["v"], None) != v:
                keys[k] = {"v": s, "t": now}
                changed = True
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = load_cfg()
    if not cfg:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M")
    st = jload(STATE.read_text(encoding="utf-8") if STATE.exists() else "", {})
    try:
        remote = read_remote(cfg)
    except Exception as e:  # 網路、token 過期都只記一行，不擋後面的報告
        print(f"[sync_holdings {stamp}] 讀不到同步資料：{type(e).__name__} {str(e)[:80]}")
        return
    keys = remote.get("keys") or {}
    local = load_toml()
    rts = {k: (keys.get(k) or {}).get("t") for k in [f"pos_{m}" for m in MARKETS]}
    toml_changed = canon(local) != st.get("toml")
    remote_changed = rts != st.get("remote")
    msg = "沒變"
    if toml_changed and local:
        now = int(time.time() * 1000)
        if apply_toml(keys, local, now):
            msg = "持股檔 → 網頁"
            if not a.dry_run:
                write_remote(cfg, keys)
        rts = {k: (keys.get(k) or {}).get("t") for k in rts}
    elif remote_changed:
        h = {**local, **from_remote(keys)}
        if h and canon(h) != canon(local):
            msg = "網頁 → 持股檔"
            if not a.dry_run:
                TOML.write_text(dump_toml(h), encoding="utf-8")
            local = h
    if a.dry_run:
        print(f"[sync_holdings] dry-run：{msg}")
        return
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"toml": canon(local), "remote": rts, "at": stamp}), encoding="utf-8")
    if msg != "沒變":
        print(f"[sync_holdings {stamp}] {msg}")


if __name__ == "__main__":
    sys.exit(main())
