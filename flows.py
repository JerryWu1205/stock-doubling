"""台股三大法人個股買賣超（上市，TWSE T86）。

每次 rotation.py 跑台股時補抓幾天的歷史（一天一個請求，避免被證交所擋），
存在 cache/tw/inst.pkl（不進 git）：{日期: DataFrame(index=代號, columns=[fi, it])}，單位是股。
fi＝外陸資（不含外資自營商）買賣超，it＝投信買賣超。
"""
import time
import traceback

import pandas as pd

import fundamentals as fu

T86 = "https://www.twse.com.tw/rwd/zh/fund/T86?date={d}&selectType=ALLBUT0999&response=json"


def _col(fields, *keys):
    for i, f in enumerate(fields):
        if any(k in f for k in keys):
            return i
    return None


def fetch_day(d):
    """d: pandas Timestamp。回傳 DataFrame 或 None（休市、還沒公布）。"""
    j = fu._get_json(T86.format(d=d.strftime("%Y%m%d")))
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    f = j["fields"]
    ic, ifi, iit = _col(f, "證券代號"), _col(f, "外陸資買賣超"), _col(f, "投信買賣超")
    if None in (ic, ifi, iit):
        raise ValueError(f"T86 欄位變了：{f}")
    rows = {str(r[ic]).strip(): (fu._num(r[ifi]) or 0.0, fu._num(r[iit]) or 0.0) for r in j["data"]}
    return pd.DataFrame.from_dict(rows, orient="index", columns=["fi", "it"])


def update(cache_dir, dates, max_new=25, probe=None):
    """補抓 dates（交易日）裡還沒有的日子，最新的先抓。回傳 {日期: DataFrame}。"""
    path = cache_dir / "inst.pkl"
    try:
        h = pd.read_pickle(path)
    except Exception:
        h = {"days": {}, "empty": []}
    days, empty = h["days"], set(h.get("empty", []))
    todo = [d for d in sorted(dates, reverse=True) if d not in days and d not in empty][:max_new]
    n = 0
    for d in todo:
        try:
            df = fetch_day(d)
        except Exception:
            if probe is not None:
                probe["tw_inst_error"] = traceback.format_exc()[-300:]
            break
        if df is None:
            empty.add(d)
        else:
            days[d] = df
            n += 1
        time.sleep(2)
    keep = set(dates)
    h = {"days": {d: v for d, v in days.items() if d in keep}, "empty": sorted(e for e in empty if e in keep)}
    if todo:
        cache_dir.mkdir(parents=True, exist_ok=True)
        pd.to_pickle(h, path)
    if probe is not None:
        probe["tw_inst"] = {"have": len(h["days"]), "want": len(dates), "new": n}
    return h["days"]


def net_value(days, close, codes):
    """每天的（外資＋投信）買賣超金額（股數 × 收盤價），DataFrame(日期 × 代號)，沒資料的日子是 NaN。"""
    idx = close.index
    sh = pd.DataFrame(index=idx, columns=codes, dtype=float)
    for d, df in days.items():
        if d in sh.index:
            s = (df["fi"] + df["it"]).reindex(codes)
            sh.loc[d] = s.to_numpy(dtype=float)
    return sh * close[codes]
