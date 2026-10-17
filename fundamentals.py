"""基本面與市場資料：大盤儀表板、月營收、估值、融資融券、借券賣出、除權息、財報日。

資料來源（都是免費公開資料）：
  台股上市：證交所 OpenAPI（openapi.twse.com.tw）和證交所網站 JSON（www.twse.com.tw/rwd）
  台股上櫃：櫃買中心 OpenAPI（www.tpex.org.tw/openapi）
  美股與指數：Yahoo Finance（yfinance）

只供網頁顯示，不進評分。任何一項抓失敗都只記在 logs/fund_probe.json，不影響掃描。
輸出：docs/data/dash.json（大盤儀表板）、docs/data/fund/<市場>.json（個股基本面）
"""

import json
import time as _time
import traceback
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "docs" / "data"
STATE = HERE / "state"
PROBE = HERE / "logs" / "fund_probe.json"
UA = {"User-Agent": "Mozilla/5.0 (stock-doubling scanner)"}

TWSE = "https://openapi.twse.com.tw/v1"
TWSE_WEB = "https://www.twse.com.tw/rwd/zh"
TPEX = "https://www.tpex.org.tw/openapi/v1"

_probe = {}


def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8-sig"))


def _fetch(name, url):
    """抓一個資料源，記下欄位名稱和錯誤，方便之後檢查格式有沒有變。"""
    try:
        data = _get_json(url)
        first = data[0] if isinstance(data, list) and data else data
        _probe[name] = {"ok": True, "n": len(data) if isinstance(data, list) else None,
                        "keys": list(first.keys())[:40] if isinstance(first, dict) else None,
                        "sample": first if isinstance(first, dict) and len(json.dumps(first)) < 1500 else None}
        return data
    except Exception as e:
        _probe[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        return None


def _num(v):
    if v is None:
        return None
    s = str(v).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "N/A", "X"):
        return None
    try:
        x = float(s)
        return x if np.isfinite(x) else None
    except ValueError:
        return None


def _pick(rec, *cands):
    """欄位名稱各家寫法不一，依序找第一個存在的（也接受「包含」比對）。"""
    for c in cands:
        if c in rec:
            return rec[c]
    for c in cands:
        for k in rec:
            if c in k:
                return rec[k]
    return None


def _roc_to_iso(s):
    s = str(s or "").strip().replace("/", "")
    if len(s) == 7 and s.isdigit():  # 1151016
        return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
    if len(s) == 5 and s.isdigit():  # 11509（年月）
        return f"{int(s[:3]) + 1911}-{s[3:5]}"
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s or None


def _r(x, n=2):
    return None if x is None or not np.isfinite(x) else round(float(x), n)


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save(path, obj, compact=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":") if compact else None,
                  indent=None if compact else 1)
    tmp.replace(path)


# ───────── 大盤儀表板 ─────────

DASH = [
    ("^SOX", "費城半導體", "idx"),
    ("^VIX", "VIX 恐慌指數", "idx"),
    ("^TNX", "美 10 年債殖利率", "yield"),
    ("TWD=X", "美元兌台幣", "fx"),
    ("^TWII", "台股加權", "idx"),
    ("^GSPC", "S&P 500", "idx"),
    ("BZ=F", "布蘭特原油", "oil"),
    ("CL=F", "WTI 原油", "oil"),
]


def dashboard(yf_download, now_utc):
    syms = [s for s, _, _ in DASH] + ["TSM", "2330.TW"]
    data = yf_download(syms, period="5y", interval="1d")
    items = []

    def hist(c):
        """點進去看的走勢圖：近一年日線、近五年週線。"""
        d = c.iloc[-252:]
        w = c.resample("W-FRI").last().dropna().iloc[-262:]
        return {"d": {"t": [x.date().isoformat() for x in d.index], "v": [_r(float(x), 3) for x in d]},
                "w": {"t": [x.date().isoformat() for x in w.index], "v": [_r(float(x), 3) for x in w]}}

    for s, label, kind in DASH:
        df = data.get(s)
        if df is None or df.empty:
            continue
        c = df["Close"].dropna()
        if len(c) < 2:
            continue
        last, prev = float(c.iloc[-1]), float(c.iloc[-2])
        if kind == "yield" and last > 20:  # ^TNX 有時候是 x10
            c = c / 10
            last, prev = last / 10, prev / 10
        sma200 = float(c.iloc[-200:].mean()) if len(c) >= 150 else None
        items.append({"sym": s, "label": label, "kind": kind, "value": _r(last, 3 if kind == "fx" else 2),
                      "chg": _r(last - prev, 3) if kind in ("yield", "fx") else None,
                      "chg_pct": _r((last / prev - 1) * 100),
                      "vs_sma200_pct": _r((last / sma200 - 1) * 100, 1) if sma200 else None,
                      "spark": [_r(float(x), 3) for x in c.iloc[-60:]], "hist": hist(c)})
    # 台積電 ADR 溢價：1 股 ADR = 5 股台股
    try:
        adr, tw, fx = data.get("TSM"), data.get("2330.TW"), data.get("TWD=X")
        a = adr["Close"].dropna()
        t = tw["Close"].dropna()
        f = fx["Close"].dropna()
        idx = a.index.intersection(t.index)
        f = f.reindex(idx, method="ffill")
        prem = (a.reindex(idx) / (t.reindex(idx) * 5 / f) - 1) * 100
        prem = prem.dropna()
        if len(prem):
            items.append({"sym": "TSM_PREM", "label": "台積電 ADR 溢價", "kind": "prem",
                          "value": _r(float(prem.iloc[-1])), "chg": _r(float(prem.iloc[-1] - prem.iloc[-2]))
                          if len(prem) > 1 else None,
                          "avg_1y": _r(float(prem.iloc[-252:].mean())), "spark": [_r(float(x)) for x in prem.iloc[-60:]],
                          "hist": hist(prem),
                          "note": "TSM ÷（2330 × 5 ÷ 美元台幣）− 1；日期以兩邊都有收盤的那天對齊"})
    except Exception:
        _probe["tsm_premium"] = {"ok": False, "error": traceback.format_exc()[-300:]}
    out = {"generated_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"), "items": items}
    _save(DATA_DIR / "dash.json", out)
    return out


# ───────── 台股 ─────────

def tw_month_revenue():
    out = {}
    for name, url in (("twse_revenue", f"{TWSE}/opendata/t187ap05_L"),
                      ("tpex_revenue", f"{TPEX}/mopsfin_t187ap05_O")):
        for r in _fetch(name, url) or []:
            code = str(_pick(r, "公司代號", "SecuritiesCompanyCode") or "").strip()
            if not code:
                continue
            out[code] = {"ym": _roc_to_iso(_pick(r, "資料年月")),
                         "rev": _num(_pick(r, "營業收入-當月營收", "當月營收")),
                         "mom": _r(_num(_pick(r, "營業收入-上月比較增減(%)", "上月比較增減"))),
                         "yoy": _r(_num(_pick(r, "營業收入-去年同月增減(%)", "去年同月增減"))),
                         "cum_yoy": _r(_num(_pick(r, "累計營業收入-前期比較增減(%)", "前期比較增減")))}
    return out


def tw_valuation_today():
    out = {}
    for r in _fetch("twse_valuation", f"{TWSE}/exchangeReport/BWIBBU_ALL") or []:
        code = str(r.get("Code", "")).strip()
        if code:
            out[code] = {"pe": _num(r.get("PEratio")), "pb": _num(r.get("PBratio")),
                         "dy": _num(r.get("DividendYield")), "date": _roc_to_iso(r.get("Date"))}
    for r in _fetch("tpex_valuation", f"{TPEX}/tpex_mainboard_peratio_analysis") or []:
        code = str(_pick(r, "SecuritiesCompanyCode", "股票代號", "代號") or "").strip()
        if code:
            out[code] = {"pe": _num(_pick(r, "PriceEarningRatio", "本益比")),
                         "pb": _num(_pick(r, "PriceBookRatio", "股價淨值比")),
                         "dy": _num(_pick(r, "YieldRatio", "殖利率")),
                         "date": _roc_to_iso(_pick(r, "Date", "日期"))}
    return out


def tw_valuation_history(codes, max_calls=6):
    """上市股票每月底的本益比、淨值比、殖利率（證交所 BWIBBU_d），回補 5 年，每次最多抓 max_calls 個月。"""
    path = STATE / "tw" / "valuation_hist.json"
    hist = _load(path, {})
    today = date.today()
    months = []
    for i in range(1, 61):
        y, m = today.year, today.month - i
        while m <= 0:
            y, m = y - 1, m + 12
        months.append(f"{y}{m:02d}")
    todo = [m for m in months if m not in hist][:max_calls]
    for ym in todo:
        # 該月最後一個交易日：從月底往前找，最多試 6 天
        y, m = int(ym[:4]), int(ym[4:])
        last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
        got = None
        for back in range(7):
            d = last - timedelta(days=back)
            if d.weekday() >= 5:
                continue
            try:
                j = _get_json(f"{TWSE_WEB}/afterTrading/BWIBBU_d?date={d:%Y%m%d}&selectType=ALL&response=json")
                _time.sleep(3)  # 證交所網站有頻率限制
            except Exception as e:
                _probe["twse_valuation_hist"] = {"ok": False, "error": str(e)[:200]}
                _time.sleep(5)
                continue
            if j.get("stat") != "OK" or not j.get("data"):
                continue
            f = j.get("fields", [])
            ix = {k: next((i for i, x in enumerate(f) if k in x), None) for k in ("代號", "本益比", "淨值比", "殖利率")}
            if ix["代號"] is None:
                _probe["twse_valuation_hist"] = {"ok": False, "fields": f}
                break
            got = {}
            for row in j["data"]:
                code = str(row[ix["代號"]]).strip()
                if code in codes:
                    got[code] = [_num(row[ix["本益比"]]) if ix["本益比"] is not None else None,
                                 _num(row[ix["淨值比"]]) if ix["淨值比"] is not None else None,
                                 _num(row[ix["殖利率"]]) if ix["殖利率"] is not None else None]
            _probe["twse_valuation_hist"] = {"ok": True, "fields": f}
            break
        hist[ym] = got or {}
    if todo:
        _save(path, hist, compact=False)
    return hist


def _pct_rank(series, x):
    v = [s for s in series if s is not None and np.isfinite(s) and s > 0]
    if x is None or len(v) < 6:
        return None
    return round(float(np.mean([s <= x for s in v])) * 100)


def tw_margin():
    out = {}
    for r in _fetch("twse_margin", f"{TWSE}/exchangeReport/MI_MARGN") or []:
        code = str(r.get("股票代號", "")).strip()
        if not code:
            continue
        fin, fin0 = _num(r.get("融資今日餘額")), _num(r.get("融資前日餘額"))
        sh, sh0 = _num(r.get("融券今日餘額")), _num(r.get("融券前日餘額"))
        out[code] = {"fin": fin, "fin_chg": None if fin is None or fin0 is None else fin - fin0,
                     "short": sh, "short_chg": None if sh is None or sh0 is None else sh - sh0,
                     "ratio": _r(sh / fin * 100, 1) if fin and sh is not None else None}
    for r in _fetch("tpex_margin", f"{TPEX}/tpex_mainboard_margin_balance") or []:
        code = str(_pick(r, "SecuritiesCompanyCode", "代號") or "").strip()
        if not code:
            continue
        fin = _num(_pick(r, "MarginPurchaseBalance", "融資餘額", "資餘額"))
        fin0 = _num(_pick(r, "MarginPurchaseBalancePreviousDay", "前資餘額", "前日融資餘額"))
        sh = _num(_pick(r, "ShortSaleBalance", "融券餘額", "券餘額"))
        sh0 = _num(_pick(r, "ShortSaleBalancePreviousDay", "前券餘額", "前日融券餘額"))
        out[code] = {"fin": fin, "fin_chg": None if fin is None or fin0 is None else fin - fin0,
                     "short": sh, "short_chg": None if sh is None or sh0 is None else sh - sh0,
                     "ratio": _r(sh / fin * 100, 1) if fin and sh is not None else None}
    return out


def tw_sbl():
    """借券賣出餘額（上市，證交所 TWT93U：融券借券賣出餘額）。往前找最近一個有資料的交易日。"""
    out = {}
    d = date.today()
    for back in range(6):
        day = d - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        try:
            j = _get_json(f"{TWSE_WEB}/marginTrading/TWT93U?date={day:%Y%m%d}&response=json")
        except Exception as e:
            _probe["twse_sbl"] = {"ok": False, "error": str(e)[:200]}
            return out
        if j.get("stat") != "OK" or not j.get("data"):
            continue
        f = j.get("fields", [])
        _probe["twse_sbl"] = {"ok": True, "fields": f, "date": day.isoformat()}
        # 欄位：代號、名稱、融券(6 欄)、借券賣出(前日餘額、當日賣出、當日還券、當日調整、當日餘額、次一營業日可限額)
        idx_code = 0
        sbl_cols = [i for i, x in enumerate(f) if "借券" in x] or list(range(8, 14))
        for row in j["data"]:
            code = str(row[idx_code]).strip()
            try:
                prev = _num(row[8]) if len(row) > 12 else None
                bal = _num(row[12]) if len(row) > 12 else None
            except IndexError:
                continue
            out[code] = {"bal": bal, "chg": None if bal is None or prev is None else bal - prev,
                         "date": day.isoformat()}
        _probe["twse_sbl"]["sbl_cols"] = sbl_cols
        break
    return out


def tw_exdividend():
    out = {}
    for r in _fetch("twse_exdiv", f"{TWSE}/exchangeReport/TWT48U_ALL") or []:
        code = str(r.get("Code", "")).strip()
        if code:
            out.setdefault(code, {"date": _roc_to_iso(r.get("Date")), "kind": r.get("Exdividend"),
                                  "cash": _num(r.get("CashDividend"))})
    for r in _fetch("tpex_exdiv", f"{TPEX}/tpex_exright_prepost") or []:
        code = str(_pick(r, "SecuritiesCompanyCode", "代號") or "").strip()
        if code:
            out.setdefault(code, {"date": _roc_to_iso(_pick(r, "ExRrightsExDividendDate", "ExRightsExDividendDate", "除權息日期", "Date")),
                                  "kind": _pick(r, "ExRrightsExDividend", "ExRightsExDividend", "除權息"),
                                  "cash": _num(_pick(r, "CashDividend", "現金股利"))})
    return out


def tw_fundamentals(codes, now_utc):
    codes = set(codes)
    rev = tw_month_revenue()
    val = tw_valuation_today()
    hist = tw_valuation_history(codes)
    mar = tw_margin()
    sbl = tw_sbl()
    exd = tw_exdividend()
    today = date.today().isoformat()
    items = {}
    for c in codes:
        it = {}
        if c in rev:
            it["rev"] = rev[c]
        if c in val:
            v = dict(val[c])
            series = [hist[m].get(c) for m in sorted(hist) if hist[m].get(c)]
            for i, k in enumerate(("pe", "pb", "dy")):
                v[f"{k}_pct"] = _pct_rank([s[i] for s in series], v.get(k))
            v["hist_months"] = len(series)
            it["val"] = v
        if c in mar:
            it["margin"] = mar[c]
        if c in sbl:
            it["sbl"] = sbl[c]
        if c in exd and (exd[c]["date"] or "") >= today:
            it["exdiv"] = exd[c]
        if it:
            items[c] = it
    return {"market": "tw", "generated_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "items": items,
            "notes": {"val": "本益比、淨值比、殖利率；百分位＝現在在過去 5 年月底資料裡的位置（0＝最便宜，100＝最貴，殖利率相反）",
                      "margin": "融資融券單位：張", "sbl": "借券賣出餘額單位：股（只有上市）"}}


# ───────── 美股 ─────────

def us_fundamentals(tickers, now_utc, budget=20):
    """美股：估值（Yahoo）、最近一季營收年增、分析師預估、下次財報日。

    每檔要打好幾次 API，所以每次掃描最多更新 budget 檔，其他的下一次（15 分鐘後）再補，一天更新一輪。
    """
    import yfinance as yf
    path = STATE / "us" / "fund_cache.json"
    cache = _load(path, {})
    today = date.today().isoformat()
    items = {}
    for t in tickers:
        c = cache.get(t)
        if c and c.get("day") == today and c.get("v") == 2:
            items[t] = c["data"]
            continue
        if budget <= 0:
            if c:
                items[t] = c["data"]
            continue
        budget -= 1
        it = {}
        try:
            tk = yf.Ticker(t)
            info = tk.info or {}
            # 殖利率：用每股股利 ÷ 股價自己算，yfinance 的 dividendYield 各版本單位不一致
            px_ = info.get("currentPrice") or info.get("regularMarketPrice")
            rate = info.get("dividendRate") or info.get("trailingAnnualDividendRate")
            dy = rate / px_ if rate and px_ else None
            it["val"] = {"pe": _r(info.get("trailingPE")), "fpe": _r(info.get("forwardPE")),
                         "pb": _r(info.get("priceToBook")), "dy": _r(dy * 100) if dy else None}
            try:
                q = tk.quarterly_income_stmt
                if q is not None and "Total Revenue" in q.index:
                    rv = q.loc["Total Revenue"].dropna().sort_index()
                    if len(rv) >= 5:
                        it["rev"] = {"q": str(rv.index[-1].date()), "rev": float(rv.iloc[-1]),
                                     "yoy": _r((rv.iloc[-1] / rv.iloc[-5] - 1) * 100),
                                     "qoq": _r((rv.iloc[-1] / rv.iloc[-2] - 1) * 100)}
                    elif len(rv) >= 2:
                        it["rev"] = {"q": str(rv.index[-1].date()), "rev": float(rv.iloc[-1]),
                                     "qoq": _r((rv.iloc[-1] / rv.iloc[-2] - 1) * 100)}
            except Exception:
                pass
            # 分析師預估：下一季 EPS、營收，以及目標價
            est = {}
            try:
                ee = tk.earnings_estimate
                if ee is not None and "avg" in ee.columns:
                    for k in ("0q", "+1q", "0y", "+1y"):
                        if k in ee.index:
                            est[f"eps_{k}"] = _r(_num(ee.loc[k, "avg"]))
                            if "growth" in ee.columns:
                                g = _num(ee.loc[k, "growth"])
                                est[f"eps_{k}_g"] = _r(g * 100, 1) if g is not None else None
                re_ = tk.revenue_estimate
                if re_ is not None and "avg" in re_.columns and "0q" in re_.index:
                    est["rev_0q"] = _num(re_.loc["0q", "avg"])
                    if "growth" in re_.columns:
                        g = _num(re_.loc["0q", "growth"])
                        est["rev_0q_g"] = _r(g * 100, 1) if g is not None else None
            except Exception:
                pass
            for k_src, k in (("targetMeanPrice", "target"), ("targetHighPrice", "target_hi"),
                             ("targetLowPrice", "target_lo"), ("numberOfAnalystOpinions", "analysts"),
                             ("recommendationKey", "rating")):
                if info.get(k_src) is not None:
                    est[k] = info[k_src] if k == "rating" else _r(_num(info[k_src]))
            if est:
                it["est"] = est
            try:
                cal = tk.calendar or {}
                ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if ed:
                    d0 = ed[0] if isinstance(ed, (list, tuple)) else ed
                    it["earnings"] = {"date": str(pd.Timestamp(d0).date())}
                exd = cal.get("Ex-Dividend Date") if isinstance(cal, dict) else None
                if exd and str(pd.Timestamp(exd).date()) >= today:
                    it["exdiv"] = {"date": str(pd.Timestamp(exd).date())}
            except Exception:
                pass
        except Exception as e:
            _probe.setdefault("us_errors", []).append(f"{t}: {type(e).__name__}"[:80])
            if c:
                items[t] = c["data"]
            continue
        cache[t] = {"day": today, "v": 2, "data": it}
        items[t] = it
        _time.sleep(0.3)
    _save(path, cache, compact=True)
    # 美股估值歷史：每天記一筆，之後累積成百分位
    hp = STATE / "us" / "valuation_hist.json"
    vh = _load(hp, {})
    vh[today] = {t: [i.get("val", {}).get("pe"), i.get("val", {}).get("pb"), i.get("val", {}).get("dy")]
                 for t, i in items.items()}
    for d in sorted(vh)[:-1300]:
        del vh[d]
    _save(hp, vh)
    for t, it in items.items():
        v = it.get("val")
        if not v:
            continue
        series = [vh[d].get(t) for d in sorted(vh) if vh[d].get(t)]
        for i, k in enumerate(("pe", "pb", "dy")):
            v[f"{k}_pct"] = _pct_rank([s[i] for s in series], v.get(k))
        v["hist_days"] = len(series)
    return {"market": "us", "generated_at": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "items": items,
            "notes": {"val": "本益比（近四季）、預估本益比、淨值比、殖利率；歷史位置從開始記錄那天起累積",
                      "rev": "美股沒有月營收，顯示最近一季營收的年增率（YoY）和季增率（QoQ）"}}


def run(cfg_key, tickers, yf_download, now_utc, force=False, dash=True):
    """每個市場一天更新一次（或沒有檔案時）；儀表板每次都更新。失敗不影響掃描。"""
    global _probe
    _probe = _load(PROBE, {})
    # 儀表板：台美股輪流跑，10 分鐘內更新過就不重抓
    d_old = _load(DATA_DIR / "dash.json", {})
    if dash and d_old.get("generated_at"):
        dash = (now_utc - datetime.fromisoformat(d_old["generated_at"].replace("Z", "+00:00"))).total_seconds() > 600
    if dash:
        try:
            dashboard(yf_download, now_utc)
        except Exception:
            _probe["dashboard"] = {"ok": False, "error": traceback.format_exc()[-400:]}
    path = DATA_DIR / "fund" / f"{cfg_key}.json"
    old = _load(path, {})
    age_h = 999
    if old.get("generated_at"):
        age_h = (now_utc - datetime.fromisoformat(old["generated_at"].replace("Z", "+00:00"))).total_seconds() / 3600
    # 台股的月營收、融資融券晚上才更新，所以 6 小時重抓一次；美股一天一次
    # 美股分批更新：快取裡還有今天沒更新到的就繼續跑
    uc = _load(STATE / "us" / "fund_cache.json", {}) if cfg_key == "us" else {}
    us_pending = cfg_key == "us" and any(
        (uc.get(t) or {}).get("v") != 2 or (uc.get(t) or {}).get("day") != date.today().isoformat()
        for t in tickers)
    if force or age_h >= (6 if cfg_key == "tw" else 20) or us_pending:
        try:
            out = tw_fundamentals(tickers, now_utc) if cfg_key == "tw" else us_fundamentals(tickers, now_utc)
            _save(path, out)
        except Exception:
            _probe[f"{cfg_key}_fund"] = {"ok": False, "error": traceback.format_exc()[-400:]}
    _probe["_updated"] = now_utc.isoformat(timespec="seconds")
    try:
        _save(PROBE, _probe, compact=False)
    except OSError:
        pass
