"""台股歷史基本面（回測用）：月營收年增率（公開資訊觀測站彙總表）、月底本益比（證交所 BWIBBU_d，只有上市）。

每次 rotation.py 跑台股時補抓幾個月，存在 cache/tw/hist.pkl（不進 git）。
月營收在次月 10 日前公布，所以回測時從次月 11 日起才算「看得到」。
"""
import re
import time
import traceback
import urllib.request

import pandas as pd

import fundamentals as fu

MOPS = "https://mops.twse.com.tw/nas/t21/{mk}/t21sc03_{y}_{m}_{k}.html"
BWI = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?date={d}&selectType=ALL&response=json"
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def _rev_month(y, m):
    """回傳 {代號: 年增率%}；還沒公布回傳 None。上市＋上櫃、本國＋外國（KY）。"""
    out, got = {}, False
    for mk in ("sii", "otc"):
        for k in (0, 1):
            url = MOPS.format(mk=mk, y=y - 1911, m=m, k=k)
            req = urllib.request.Request(url, headers=BROWSER)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    html = r.read().decode("big5", "ignore")
            except Exception:
                continue
            time.sleep(1.5)
            for tr in _TR.findall(html):
                tds = [_TAG.sub("", x).replace("&nbsp;", "").strip() for x in _TD.findall(tr)]
                if len(tds) >= 7 and re.fullmatch(r"\d{4}", tds[0]):
                    yoy = fu._num(tds[6])
                    got = True
                    if yoy is not None:
                        out[tds[0]] = yoy
    return out if got else None


def _pe_day(d):
    j = fu._get_json(BWI.format(d=d.strftime("%Y%m%d")))
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    f = j["fields"]
    ic = next(i for i, x in enumerate(f) if "代號" in x)
    ip = next(i for i, x in enumerate(f) if "本益比" in x)
    return {str(r[ic]).strip(): fu._num(r[ip]) for r in j["data"]}


def update(cache_dir, dates, budget=8, probe=None):
    """dates：價格資料的交易日。回傳 (rev, pe)：
    rev＝DataFrame(公布後可用日 × 代號) 年增率；pe＝DataFrame(月底交易日 × 代號) 本益比。"""
    path = cache_dir / "hist.pkl"
    try:
        h = pd.read_pickle(path)
    except Exception:
        h = {"rev": {}, "pe": {}, "rev_none": []}
    first, last = dates[0], dates[-1]
    # 需要的月份：回測開始前 2 個月到上個月
    months = pd.period_range((first - pd.DateOffset(months=2)).to_period("M"), (last - pd.DateOffset(months=1)).to_period("M"), freq="M")
    month_ends = [d for d in pd.Series(dates).groupby(pd.Series(dates).dt.to_period("M")).max()]
    n = 0
    try:
        for p in reversed(months):
            if n >= budget:
                break
            key = str(p)
            if key in h["rev"]:
                continue
            avail = pd.Timestamp(p.year, p.month, 1) + pd.DateOffset(months=1, days=10)
            if avail > last + pd.Timedelta(days=1):
                continue
            n += 1
            r = _rev_month(p.year, p.month)
            if r:
                h["rev"][key] = r
        for d in reversed(month_ends):
            if n >= budget:
                break
            key = d.strftime("%Y-%m-%d")
            if key in h["pe"]:
                continue
            n += 1
            r = _pe_day(d)
            time.sleep(2)
            if r:
                h["pe"][key] = r
    except Exception:
        if probe is not None:
            probe["tw_hist_error"] = traceback.format_exc()[-300:]
    if n:
        cache_dir.mkdir(parents=True, exist_ok=True)
        pd.to_pickle(h, path)
    if probe is not None:
        probe["tw_hist"] = {"rev_months": len(h["rev"]), "rev_want": len(months), "pe_days": len(h["pe"]), "pe_want": len(month_ends)}
    rev = pd.DataFrame({pd.Timestamp(pd.Period(k).year, pd.Period(k).month, 1) + pd.DateOffset(months=1, days=10): v
                        for k, v in h["rev"].items()}).T.sort_index() if h["rev"] else None
    pe = pd.DataFrame({pd.Timestamp(k): v for k, v in h["pe"].items()}).T.sort_index() if h["pe"] else None
    ready = len(h["rev"]) >= len(months) - 1 and len(h["pe"]) >= len(month_ends) - 1
    return rev, pe, ready
