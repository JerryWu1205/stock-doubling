"""台股盤中即時報價（永豐金 Shioaji，只用「行情」權限）。

金鑰放在 shioaji.env（SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY），這個檔案只在這台電腦、不進 git。
沒填金鑰、沒裝 shioaji、連線失敗都直接跳過，照舊用 Yahoo（延遲約 20 分鐘）。
狀態寫在 logs/twrt_probe.json（不含金鑰），方便確認有沒有接上。
連線在獨立的子程式裡跑，超時就整個砍掉，主掃描不會被卡住。"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ENV = HERE / "shioaji.env"
PROBE = HERE / "logs" / "twrt_probe.json"


def _keys():
    env = {}
    try:
        for line in ENV.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env.get("SHIOAJI_API_KEY"), env.get("SHIOAJI_SECRET_KEY")


def _probe(status, **kw):
    try:
        PROBE.parent.mkdir(exist_ok=True)
        PROBE.write_text(json.dumps({"status": status, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **kw},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


FIELDS = ("code", "ts", "open", "high", "low", "close", "total_volume")


def _login(api, key, secret):
    try:
        api.login(api_key=key, secret_key=secret, fetch_contract=True, contracts_timeout=30000)
    except TypeError:  # 新版 shioaji 的 login 參數不同
        api.login(api_key=key, secret_key=secret)


def _snapshots(codes, key, secret):
    import shioaji as sj
    api = sj.Shioaji()
    _login(api, key, secret)
    try:
        contracts = [c for c in (api.Contracts.Stocks[x] for x in codes) if c is not None]
        out = {}
        for i in range(0, len(contracts), 400):
            for s in api.snapshots(contracts[i:i + 400]):
                out[s.code] = {f: getattr(s, f, None) for f in FIELDS}
        return out
    finally:
        try:
            api.logout()
        except Exception:
            pass


def _child(out_path):
    """子程式：從 stdin 讀代號，結果寫到 out_path，最後直接結束（不等 shioaji 的背景執行緒）。"""
    res = {}
    key, secret = _keys()
    try:
        codes = json.loads(sys.stdin.read() or "[]")
        res["snaps"] = _snapshots(codes, key, secret)
    except ImportError:
        res["err"] = "shioaji 還沒安裝（run.bat 會自動安裝）"
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:200]
        for v, tag in ((key, "<KEY>"), (secret, "<SECRET>")):
            if v:
                msg = msg.replace(v, tag)
        res["err"] = msg
    try:
        Path(out_path).write_text(json.dumps(res, default=str), encoding="utf-8")
    finally:
        os._exit(0)


class _Snap:
    def __init__(self, d):
        self.__dict__.update(d)


def snapshots(codes, timeout=120):
    """{代號: snapshot}；拿不到就回傳 {}。"""
    key, secret = _keys()
    if not key or not secret:
        _probe("no_keys", note="shioaji.env 還沒填金鑰")
        return {}
    fd, out = tempfile.mkstemp(prefix="twrt_", suffix=".json")
    os.close(fd)
    try:
        try:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", out],
                           input=json.dumps(list(codes)), text=True, timeout=timeout,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(HERE))
        except subprocess.TimeoutExpired:
            _probe("timeout", seconds=timeout)
            return {}
        try:
            res = json.loads(Path(out).read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            res = {"err": "子程式沒有回傳結果"}
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    if "err" in res:
        _probe("error", error=res["err"])
        return {}
    return {c: _Snap(d) for c, d in (res.get("snaps") or {}).items()}


def patch(raw_data, today):
    """把今天的日線換成 Shioaji 即時價（沒有今天這根就補一根）。回傳換了幾檔。"""
    codes = [t for t in raw_data if not t.endswith((".TW", ".TWO"))]
    snaps = snapshots(codes)
    n, ts_max = 0, None
    day = pd.Timestamp(today)
    for t, s in snaps.items():
        df = raw_data.get(t)
        if df is None or not s.close or s.close <= 0:
            continue
        st = pd.Timestamp(s.ts)  # Shioaji 的 ts 是台北當地時間
        if st.normalize() != day:
            continue  # 今天還沒成交
        ts_max = max(ts_max, st) if ts_max is not None else st
        row = {"Open": float(s.open or s.close), "High": float(s.high or s.close), "Low": float(s.low or s.close),
               "Close": float(s.close), "Adj Close": float(s.close), "Volume": float(s.total_volume or 0) * 1000}
        df = df.copy()
        if day in df.index:
            for k, v in row.items():
                if k in df.columns:
                    df.at[day, k] = v
        else:
            df.loc[day] = pd.Series(row).reindex(df.columns)
            df = df.sort_index()
        raw_data[t] = df
        n += 1
    if snaps:
        _probe("ok", patched=n, asked=len(codes), last_tick=str(ts_max) if ts_max is not None else None)
    return n



if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--child":
    _child(sys.argv[2])
