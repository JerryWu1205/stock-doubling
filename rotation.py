"""類股輪動：全市場（美股約兩千檔、台股上市櫃全部）依產業和題材分組，看資金流向哪裡。

另外回測兩件事：
  1. 輪動選股：每週挑最強的 3 個族群、每族群挑最強 3 檔，和「不分族群直接挑最強 9 檔」、大盤比較
  2. 槓桿 ETF：原型、2 倍、3 倍，在「趨勢向上才持有」的規則下表現如何

只供網頁顯示，不進評分。
價格快取在 cache/<市場>/px.pkl（不進 git）。第一次要分好幾輪下載（每輪 250 檔），之後每 6 小時補最新幾天。
休市時才跑，避免盤中半根 K 棒。
輸出：docs/data/rotation/<市場>.json；失敗只記在 logs/rotation_probe.json。
"""

import json
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import flows
import fundamentals as fu

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "docs" / "data"
CACHE = HERE / "cache"
PROBE = HERE / "logs" / "rotation_probe.json"
_probe = {}

US_LIST = ["https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nasdaq/nasdaq_full_tickers.json",
           "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/nyse/nyse_full_tickers.json"]

# 美股產業（Nasdaq 分類）→ 中文族群；由上往下比對，先中先贏
US_GROUPS = [
    ("Semiconductors", "半導體"),
    ("Computer Software: Prepackaged", "軟體"),
    ("Computer Software: Programming", "雲端與資訊服務"), ("EDP Services", "雲端與資訊服務"),
    ("Retail: Computer", "電腦硬體"), ("Computer Manufacturing", "電腦硬體"), ("Computer peripheral", "電腦硬體"),
    ("Telecommunications Equipment", "通訊網通設備"), ("Computer Communications", "通訊網通設備"),
    ("Radio And Television Broadcasting And Communications", "通訊網通設備"),
    ("Electronic Components", "電子零組件"), ("Electronics Distribution", "電子零組件"),
    ("Electrical Products", "電機電氣設備"),
    ("Cable & Other Pay", "媒體娛樂"), ("Broadcasting", "媒體娛樂"), ("Movies", "媒體娛樂"),
    ("Newspapers", "媒體娛樂"), ("Publishing", "媒體娛樂"), ("Books", "媒體娛樂"), ("Advertising", "媒體娛樂"),
    ("Biotechnology: Pharmaceutical", "製藥"), ("Other Pharmaceuticals", "製藥"), ("Medicinal Chemicals", "製藥"),
    ("Biotechnology: Laboratory Analytical", "檢測儀器"),
    ("Biotechnology: Electromedical", "醫材"), ("Medical/Dental", "醫材"), ("Medical Specialities", "醫材"),
    ("Medical Electronics", "醫材"), ("Ophthalmic", "醫材"),
    ("Biotechnology", "生技"),
    ("Hospital", "醫療服務"), ("Medical/Nursing", "醫療服務"), ("Misc Health", "醫療服務"),
    ("Retail-Drug", "醫療服務"),
    ("Real Estate Investment Trusts", "不動產 REITs"),
    ("Banks", "銀行"), ("Savings Institutions", "銀行"),
    ("Insur", "保險"),
    ("Investment Bankers", "券商與資產管理"), ("Investment Managers", "券商與資產管理"),
    ("Finance/Investors", "券商與資產管理"), ("Diversified Financial", "券商與資產管理"), ("Trusts", "券商與資產管理"),
    ("Finance: Consumer", "消費金融與支付"), ("Finance Companies", "消費金融與支付"),
    ("Real Estate Investment Trusts", "不動產 REITs"), ("Real Estate", "不動產 REITs"), ("Building operators", "不動產 REITs"),
    ("Oil and Gas Field Machinery", "油服"), ("Oilfield Services", "油服"),
    ("Oil/Gas Transmission", "油氣管線"), ("Natural Gas Distribution", "油氣管線"),
    ("Oil & Gas Production", "石油天然氣"), ("Integrated oil", "石油天然氣"),
    ("Coal Mining", "煤礦"),
    ("Electric Utilities", "電力公用事業"), ("Power Generation", "電力公用事業"), ("Water Supply", "電力公用事業"),
    ("Precious Metals", "黃金白銀"),
    ("Metal Mining", "金屬礦業與鋼鐵"), ("Other Metals", "金屬礦業與鋼鐵"), ("Aluminum", "金屬礦業與鋼鐵"),
    ("Steel/Iron", "金屬礦業與鋼鐵"), ("Mining & Quarrying", "金屬礦業與鋼鐵"),
    ("Chemicals", "化工"), ("Paints", "化工"), ("Plastic", "化工"),
    ("Aerospace", "航太國防"), ("Military/Government", "航太國防"), ("Ordnance", "航太國防"),
    ("Engineering & Construction", "營建與建材"), ("Water Sewer Pipeline", "營建與建材"), ("General Bldg", "營建與建材"),
    ("Building", "營建與建材"), ("Homebuilding", "營建與建材"),
    ("Machinery", "機械工業"), ("Fluid Controls", "機械工業"), ("Construction/Ag", "機械工業"),
    ("Metal Fabrications", "機械工業"), ("Industrial Specialties", "機械工業"), ("Pollution Control", "機械工業"),
    ("Tools/Hardware", "機械工業"), ("Precision Instruments", "機械工業"),
    ("Railroads", "運輸物流"), ("Trucking", "運輸物流"), ("Air Freight", "運輸物流"), ("Integrated Freight", "運輸物流"),
    ("Transportation Services", "運輸物流"), ("Marine Transportation", "運輸物流"),
    ("Auto", "汽車"), ("Motor Vehicles", "汽車"),
    ("Restaurants", "餐旅娛樂"), ("Hotels", "餐旅娛樂"), ("Amusement", "餐旅娛樂"), ("Recreational", "餐旅娛樂"),
    ("Apparel", "服飾鞋類"), ("Shoe", "服飾鞋類"), ("Clothing", "服飾鞋類"), ("Garments", "服飾鞋類"),
    ("Stores", "零售"), ("Retail", "零售"), ("Catalog", "零售"), ("Food Chains", "零售"),
    ("Beverages", "食品飲料"), ("Foods", "食品飲料"), ("Meat", "食品飲料"), ("Food Distributors", "食品飲料"),
    ("Farming", "食品飲料"),
    ("Package Goods/Cosmetics", "日用消費品"), ("Consumer Specialties", "日用消費品"), ("Home Furnishings", "日用消費品"),
    ("Consumer Electronics", "日用消費品"), ("Durable Goods", "日用消費品"),
    ("Business Services", "商業服務"), ("Diversified Commercial", "商業服務"), ("Professional", "商業服務"),
    ("Rental/Leasing", "商業服務"), ("Leasing Services", "商業服務"), ("Office Equipment", "商業服務"),
    ("Other Consumer Services", "商業服務"), ("Environmental Services", "商業服務"), ("Wholesale", "商業服務"),
    ("Containers/Packaging", "包裝與紙業"), ("Paper", "包裝與紙業"), ("Forest", "包裝與紙業"),
]

# 美股題材（資金常常是追題材，不是追官方產業）
US_THEMES = {
    "AI 晶片": ["NVDA", "AMD", "AVGO", "MRVL", "ARM", "ALAB", "CRDO", "QCOM", "INTC", "TSM"],
    "半導體設備": ["ASML", "AMAT", "LRCX", "KLAC", "TER", "ONTO", "CAMT", "NVMI", "ACMR", "UCTT", "ICHR", "AMKR"],
    "記憶體與儲存": ["MU", "WDC", "STX", "SNDK", "PSTG", "NTAP"],
    "光通訊": ["COHR", "LITE", "CIEN", "AAOI", "FN", "GLW", "MTSI", "POET", "INFN"],
    "AI 伺服器與網通": ["SMCI", "DELL", "HPE", "ANET", "CLS", "JBL", "VRT", "CSCO"],
    "新雲端與算力": ["CRWV", "NBIS", "IREN", "CIFR", "APLD", "WULF", "CORZ", "ORCL", "GLXY"],
    "核能與鈾": ["OKLO", "SMR", "NNE", "CCJ", "LEU", "BWXT", "UEC", "NXE", "UUUU"],
    "電力與電網": ["VST", "CEG", "TLN", "NRG", "GEV", "ETN", "PWR", "MYRG", "POWL", "BE"],
    "量子電腦": ["IONQ", "RGTI", "QBTS", "QUBT", "ARQQ"],
    "太空與衛星": ["RKLB", "ASTS", "LUNR", "PL", "RDW", "SATS", "IRDM", "FLY"],
    "國防與無人機": ["LMT", "NOC", "RTX", "GD", "LHX", "KTOS", "AVAV", "PLTR", "AXON", "ONDS"],
    "機器人與自駕": ["TSLA", "ISRG", "SYM", "SERV", "TER", "ROK", "ZBRA", "PONY", "MBLY"],
    "資安": ["CRWD", "PANW", "ZS", "NET", "FTNT", "S", "OKTA", "CYBR", "RBRK"],
    "AI 軟體": ["PLTR", "SNOW", "DDOG", "MDB", "NOW", "CRM", "APP", "AI", "PATH", "ADBE", "INTU"],
    "加密貨幣": ["COIN", "MSTR", "MARA", "RIOT", "HOOD", "CLSK", "HUT", "BTDR", "CRCL"],
    "減重藥 GLP-1": ["LLY", "NVO", "VKTX", "AMGN", "HIMS"],
    "大型科技七雄": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"],
    "黃金礦業": ["NEM", "AEM", "GOLD", "KGC", "AU", "WPM", "FNV", "AGI"],
    "銅與稀土": ["FCX", "SCCO", "TECK", "MP", "USAR"],
    "電商與網路平台": ["AMZN", "SHOP", "MELI", "SE", "BABA", "PDD", "UBER", "DASH", "ABNB"],
}

TW_INDUSTRY = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維", "05": "電機機械", "06": "電器電纜",
    "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學", "22": "生技醫療", "23": "油電燃氣", "24": "半導體", "25": "電腦及週邊",
    "26": "光電", "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務",
    "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務", "35": "綠能環保",
    "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}

TW_THEMES = {
    "PCB 與銅箔基板": ["2383", "6274", "6213", "3037", "8046", "3189", "2368", "4958", "3044", "2313",
                    "6269", "5469", "8358", "1815", "2367", "6153"],
    "AI 伺服器組裝": ["2317", "2382", "3231", "6669", "2356", "3706", "2376", "2377", "2353"],
    "散熱": ["3017", "3324", "2421", "3653", "6230", "8996", "3338"],
    "機殼與滑軌": ["2059", "8210", "3013", "6117", "3693"],
    "先進封裝設備": ["3131", "6187", "3583", "6640", "2467", "5443", "3680", "6196", "1560"],
    "光通訊與矽光子": ["3363", "4979", "3081", "4977", "3163", "6442", "2455", "3450", "2345"],
    "IC 設計": ["2454", "3034", "2379", "3661", "3443", "3035", "6643", "5274", "4966", "3529", "6415", "8016", "6526"],
    "晶圓代工與封測": ["2330", "2303", "5347", "6770", "3711", "2449", "6239", "6147"],
    "記憶體": ["2408", "2344", "2337", "3260", "8299", "4967", "3006"],
    "重電與電網": ["1503", "1513", "1514", "1519", "1504", "1609", "1605", "1608"],
    "電源與儲能": ["2308", "2301", "6409", "3211", "6781", "2457", "6282", "3023"],
    "被動元件": ["2327", "2492", "3026", "2478", "6173", "2375"],
    "貨櫃與散裝航運": ["2603", "2609", "2615", "2637", "2606", "5608"],
    "航空": ["2618", "2610", "2633", "2645"],
    "軍工與無人機": ["2634", "8033", "2208", "3005", "6753", "4572"],
    "機器人與自動化": ["2049", "1590", "4562", "8374", "2359", "6215", "4583"],
    "新藥": ["6446", "1795", "4743", "6472", "4147", "1760", "4174", "6547", "4123"],
    "低軌衛星": ["3491", "6285", "2314", "3380", "2312"],
    "金控": ["2881", "2882", "2891", "2886", "2884", "2885", "2880", "2892", "5880", "2887", "2883", "2890"],
}

# 槓桿 ETF：(原型, [(真實槓桿 ETF, 倍數)], 說明)
LEV = {
    "us": [("QQQ", [("QLD", 2), ("TQQQ", 3)], "那斯達克 100"),
           ("SOXX", [("USD", 2), ("SOXL", 3)], "費城半導體"),
           ("SPY", [("SSO", 2), ("UPRO", 3)], "S&P 500")],
    "tw": [("0050.TW", [("00631L.TW", 2)], "台灣 50")],
}
COST = {"us": 0.0005, "tw": 0.0025}   # 單邊（台股含一半的交易稅概算）
MIN_DV = {"us": 20e6, "tw": 50e6}     # 20 日平均成交金額門檻
MIN_PX = {"us": 5, "tw": 10}


def _now_iso(now_utc):
    return now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")


def _age_h(iso, now_utc):
    if not iso:
        return 1e9
    return (now_utc - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600


# ───────── 名單 ─────────

def _us_universe():
    rows = []
    for u in US_LIST:
        data = fu._get_json(u, timeout=60)
        rows += data
    out = {}
    for r in rows:
        s = str(r.get("symbol", "")).strip().upper()
        if not s or any(c in s for c in "^/ ") or len(s) > 5:
            continue
        cap = fu._num(r.get("marketCap")) or 0
        px = fu._num(str(r.get("lastsale", "")).replace("$", "")) or 0
        if cap < 2e9 or px < MIN_PX["us"]:
            continue
        ind = (r.get("industry") or "").strip()
        grp = next((g for k, g in US_GROUPS if k.lower() in ind.lower()), "其他")
        name = str(r.get("name") or "")
        for cut in (" Common Stock", " Class A", " Ordinary Shares", " American Depositary", " Inc.", ",", " Corporation"):
            i = name.find(cut)
            if i > 0:
                name = name[:i]
        out[s.replace(".", "-")] = {"name": name.strip()[:28], "ind": grp, "cap": cap}
    _probe["us_universe"] = {"ok": True, "n": len(out)}
    return out


def _tw_universe():
    out = {}
    for name, url, suffix in (("twse_list", f"{fu.TWSE}/opendata/t187ap03_L", ".TW"),
                              ("tpex_list", f"{fu.TPEX}/mopsfin_t187ap03_O", ".TWO")):
        try:
            rows = fu._get_json(url)
            first = rows[0] if rows else {}
            _probe[name] = {"ok": True, "n": len(rows), "keys": list(first.keys())[:12]}
        except Exception as e:
            _probe[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
            continue
        for r in rows:
            code = str(fu._pick(r, "公司代號", "SecuritiesCompanyCode", "CompanyCode") or "").strip()
            if not (len(code) == 4 and code.isdigit()):
                continue
            ind = str(fu._pick(r, "產業別", "SecuritiesIndustryCode", "IndustryCode") or "").strip().zfill(2)
            nm = fu._pick(r, "公司簡稱", "CompanyAbbreviation", "CompanyName", "公司名稱") or code
            out[code] = {"name": str(nm).strip(), "ind": TW_INDUSTRY.get(ind, "其他"), "sym": code + suffix}
    return out


def universe(key, now_utc):
    path = CACHE / key / "universe.json"
    old = fu._load(path, {})
    if old.get("items") and _age_h(old.get("at"), now_utc) < 24 * 7:
        return old["items"]
    items = _us_universe() if key == "us" else _tw_universe()
    if len(items) < 200:  # 抓失敗就沿用舊的
        return old.get("items") or {}
    fu._save(path, {"at": _now_iso(now_utc), "items": items})
    return items


# ───────── 價格快取 ─────────

def _pull(yf_download, syms, period, chunk):
    close, vol = {}, {}
    for i in range(0, len(syms), chunk):
        part = syms[i:i + chunk]
        try:
            d = yf_download(part, period=period, interval="1d")
        except Exception as e:
            _probe.setdefault("download_errors", []).append(f"{type(e).__name__}: {e}"[:120])
            continue
        for s, df in d.items():
            c = df["Adj Close"] if "Adj Close" in df else df["Close"]
            close[s], vol[s] = c, df["Close"] * df["Volume"]  # 成交金額
    return pd.DataFrame(close), pd.DataFrame(vol)


def update_prices(key, syms, yf_download, now_utc, max_new=250):
    """回傳 (close, dollar_volume, 有沒有變動)。"""
    path = CACHE / key / "px.pkl"
    try:
        px = pd.read_pickle(path)
    except Exception:
        px = {"close": pd.DataFrame(), "dv": pd.DataFrame(), "at": None, "failed": []}
    close, dv = px["close"], px["dv"]
    failed = set(px.get("failed", []))
    changed = False
    new = [s for s in syms if s not in close.columns and s not in failed][:max_new]
    if new:
        c, v = _pull(yf_download, new, "1y", 125)
        close = pd.concat([close, c], axis=1) if len(close.columns) else c
        dv = pd.concat([dv, v], axis=1) if len(dv.columns) else v
        failed |= set(new) - set(c.columns)
        changed = True
    elif len(close.columns) and _age_h(px.get("at"), now_utc) >= 6:
        have = [s for s in syms if s in close.columns]
        c, v = _pull(yf_download, have, "1mo", 250)
        if len(c.columns):
            close = c.combine_first(close)
            dv = v.combine_first(dv)
            # 有時候某檔沒抓到最新的一天，前一天補上，不讓它變成空值
            px["at"] = _now_iso(now_utc)
            changed = True
    if not new and px.get("at") is None and len(close.columns):
        px["at"] = _now_iso(now_utc)
    close = close.sort_index().loc[:, ~close.columns.duplicated()]
    dv = dv.sort_index().loc[:, ~dv.columns.duplicated()]
    keep = close.index[-300:]
    close, dv = close.loc[keep], dv.reindex(keep)
    if changed or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.to_pickle({"close": close, "dv": dv, "at": px.get("at"), "failed": sorted(failed)}, path)
    pending = len([s for s in syms if s not in close.columns and s not in failed])
    return close, dv, changed, pending


# ───────── 族群計算 ─────────

def _flow(close, dv, n=20):
    """資金淨流入比：近 n 天上漲日成交金額減下跌日，除以總成交金額（-1～1）。"""
    sign = np.sign(close.diff())
    return (sign * dv).rolling(n).sum() / dv.rolling(n).sum()


def group_table(close, dv, bench, members, at=-1):
    """members: {族群: [代號]}。回傳每個族群的指標（at = 第幾根 K 棒）。"""
    c = close.iloc[: len(close) + at + 1] if at != -1 else close
    d = dv.reindex(c.index)
    b = bench.reindex(c.index).ffill()
    last = c.iloc[-1]

    def ret(n):
        return last / c.iloc[-1 - n] - 1 if len(c) > n else last * np.nan

    r5, r21, r63 = ret(5), ret(21), ret(63)
    b5, b21, b63 = (float(b.iloc[-1] / b.iloc[-1 - n] - 1) if len(b) > n else 0 for n in (5, 21, 63))
    above = last > c.iloc[-50:].mean()
    fl = _flow(c.iloc[-25:], d.iloc[-25:]).iloc[-1]
    dv20, dv60 = d.iloc[-20:].mean(), d.iloc[-60:].mean()
    tot_heat = dv20.sum() / max(dv60.sum(), 1)
    rows = {}
    for g, tks in members.items():
        tks = [t for t in tks if t in c.columns and np.isfinite(r63.get(t, np.nan))]
        if len(tks) < 3:
            continue
        heat = dv20[tks].sum() / max(dv60[tks].sum(), 1) / tot_heat
        rows[g] = {"n": len(tks), "r5": float(r5[tks].median() - b5), "r21": float(r21[tks].median() - b21),
                   "r63": float(r63[tks].median() - b63), "breadth": float(above[tks].mean()),
                   "flow": float(fl[tks].median()), "heat": float(heat), "tks": tks}
    if not rows:
        return {}
    df = pd.DataFrame(rows).T
    rk = lambda col: df[col].astype(float).rank(pct=True)
    score = (0.35 * rk("r21") + 0.30 * rk("r63") + 0.10 * rk("r5") + 0.15 * rk("flow") + 0.10 * rk("breadth")) * 100
    for g in rows:
        rows[g]["score"] = float(score[g])
    order = sorted(rows, key=lambda g: -rows[g]["score"])
    for i, g in enumerate(order):
        rows[g]["rank"] = i + 1
    return rows


def leaders(close, tks, k=3):
    c = close[tks]
    last = c.iloc[-1]
    r63 = last / c.iloc[-64] - 1 if len(c) > 64 else last * np.nan
    r21 = last / c.iloc[-22] - 1
    above = last > c.iloc[-50:].mean()
    sc = (r63.rank(pct=True) + r21.rank(pct=True)).where(above)
    return [t for t in sc.sort_values(ascending=False).dropna().index[:k]]


# ───────── 回測 ─────────

def _stats(eq):
    eq = eq.dropna()
    if len(eq) < 20:
        return {}
    n = len(eq)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (252 / n) - 1
    dd = (eq / eq.cummax() - 1).min()
    return {"ret_pct": round(float(eq.iloc[-1] / eq.iloc[0] - 1) * 100, 1), "cagr_pct": round(float(cagr) * 100, 1),
            "max_dd_pct": round(float(dd) * 100, 1)}


def rotation_backtest(close, dv, bench, members, key, k_groups=3, k_each=3, step=5, start=70):
    """每 step 天換一次：最強 k_groups 個族群 × 每族群最強 k_each 檔，等權重。"""
    rets = close.pct_change(fill_method=None).fillna(0)
    idx = close.index
    eq_rot, eq_mom, dates = [1.0], [1.0], [idx[start]]
    prev_rot, prev_mom = set(), set()
    cost = COST[key]
    for t in range(start, len(idx) - 1, step):
        sub = close.iloc[: t + 1]
        tab = group_table(sub, dv.iloc[: t + 1], bench, members)
        top = sorted(tab, key=lambda g: -tab[g]["score"])[:k_groups]
        pick = []
        for g in top:
            pick += [x for x in leaders(sub, tab[g]["tks"], k_each) if x not in pick]
        last = sub.iloc[-1]
        r63 = (last / sub.iloc[-64] - 1).where(last > sub.iloc[-50:].mean()).dropna()
        mom = list(r63.sort_values(ascending=False).index[: k_groups * k_each])
        end = min(t + step, len(idx) - 1)
        for pk, prev, eq in ((pick, prev_rot, eq_rot), (mom, prev_mom, eq_mom)):
            seg = (1 + rets.iloc[t + 1: end + 1][pk].mean(axis=1)).prod() if pk else 1.0
            turn = len(set(pk) ^ prev) / max(len(pk), 1)
            eq.append(eq[-1] * seg * (1 - cost * turn))
        prev_rot, prev_mom = set(pick), set(mom)
        dates.append(idx[end])
    s_rot, s_mom = pd.Series(eq_rot, dates), pd.Series(eq_mom, dates)
    bb = bench.reindex(dates).ffill()
    def st(s):
        return _stats(s.reindex(idx).ffill())
    return {"rotation": st(s_rot), "momentum": st(s_mom), "bench": _stats(bench.reindex(idx).ffill().loc[dates[0]:]),
            "curve": {"dates": [d.date().isoformat() for d in dates],
                      "rotation": [round(float(v), 4) for v in s_rot], "momentum": [round(float(v), 4) for v in s_mom],
                      "bench": [round(float(v / bb.iloc[0]), 4) for v in bb]},
            "rule": f"每 {step} 個交易日換一次：最強 {k_groups} 個族群、每族群最強 {k_each} 檔，等權重；含交易成本"}


def count_backtest(close, dv, bench, key, ns=(5, 8, 16), step=5, start=70, groups=None, inst=None):
    """全市場（不是手挑的股票池）測持股檔數：每 step 天選相對強度＋資金流最高的前 N 檔（要站上 50 日線），等權重、含成本。
    另外把期間切成前後兩半，看結論是不是兩段都成立。"""
    rets = close.pct_change(fill_method=None).fillna(0).clip(-0.5, 1.0)
    flow = _flow(close, dv)
    idx = close.index
    # 另外三種選法（都是 16 檔）：只看 3 個月漲幅、加族群強度、加族群強度但不看 1 個月
    gof = {t: g for g, ts in (groups or {}).items() for t in ts}
    variants = ["16_r63", "16_grp", "16_grp_no21"] if gof else ["16_r63"]
    if inst is not None:
        variants += ["16_inst"]
    keys = [str(n) for n in ns] + variants
    eq = {k: [1.0] for k in keys}
    prev = {k: set() for k in keys}
    dates = [idx[start]]
    cost = COST[key]
    for t in range(start, len(idx) - 1, step):
        sub = close.iloc[: t + 1]
        last = sub.iloc[-1]
        ok = last > sub.iloc[-50:].mean()
        r63 = (last / sub.iloc[-64] - 1)[ok].dropna()
        r21 = (last / sub.iloc[-22] - 1).reindex(r63.index)
        fl = flow.iloc[t].reindex(r63.index)
        sc = (r63.rank(pct=True) * 0.45 + r21.rank(pct=True) * 0.30 + fl.rank(pct=True) * 0.25).dropna()
        order = list(sc.sort_values(ascending=False).index)
        picks = {str(n): order[:n] for n in ns}
        picks["16_r63"] = list(r63.sort_values(ascending=False).index[:16])
        if gof:
            # 族群強度：族群成員（全部，不只站上 50 日線的）3 個月漲幅中位數的排名
            all63 = (last / sub.iloc[-64] - 1).dropna()
            gm = all63.groupby(all63.index.map(lambda x: gof.get(x, "_"))).median().drop("_", errors="ignore")
            gr = gm.rank(pct=True)
            gs = pd.Series([gr.get(gof.get(x), 0.5) for x in sc.index], sc.index)
            picks["16_grp"] = list((sc * 0.8 + gs * 0.2).sort_values(ascending=False).index[:16])
            sc2 = (r63.rank(pct=True) * 0.6 + fl.rank(pct=True) * 0.4).dropna()
            gs2 = pd.Series([gr.get(gof.get(x), 0.5) for x in sc2.index], sc2.index)
            picks["16_grp_no21"] = list((sc2 * 0.8 + gs2 * 0.2).sort_values(ascending=False).index[:16])
        if inst is not None:
            # 外資＋投信近 20 天買賣超金額占成交金額的比例，排名；沒資料（上櫃）給中間值
            ir = inst.iloc[t].reindex(sc.index).rank(pct=True).fillna(0.5)
            picks["16_inst"] = list((sc * 0.8 + ir * 0.2).sort_values(ascending=False).index[:16])
        end = min(t + step, len(idx) - 1)
        for k in keys:
            pk = picks[k]
            seg = (1 + rets.iloc[t + 1: end + 1][pk].mean(axis=1)).prod() if pk else 1.0
            turn = len(set(pk) ^ prev[k]) / max(len(pk), 1)
            eq[k].append(eq[k][-1] * seg * (1 - cost * turn))
            prev[k] = set(pk)
        dates.append(idx[end])
    out = {}
    for k in keys:
        s = pd.Series(eq[k], dates).reindex(idx).ffill().loc[dates[0]:]
        h = len(s) // 2
        out[k] = {**_stats(s), "h1": _stats(s.iloc[: h + 1]).get("cagr_pct"), "h2": _stats(s.iloc[h:]).get("cagr_pct")}
    bs = bench.reindex(idx).ffill().loc[dates[0]:]
    h = len(bs) // 2
    out["bench"] = {**_stats(bs), "h1": _stats(bs.iloc[: h + 1]).get("cagr_pct"), "h2": _stats(bs.iloc[h:]).get("cagr_pct")}
    out["rule"] = (f"全市場每 {step} 個交易日換一次：站上 50 日線的股票裡，近 3 月漲幅 45%＋近 1 月 30%＋資金流 25% 排名最高的前 N 檔，"
                   "等權重、含交易成本；h1／h2 是前半段／後半段的年化。16_r63＝只看 3 個月漲幅；16_grp＝加族群強度 20%；"
                   "16_grp_no21＝加族群強度、不看 1 個月漲幅；16_inst＝加外資＋投信近 20 天買賣超 20%（只有上市股）")
    return out


def lev_backtest(key, yf_download):
    """原型、2 倍、3 倍：買進持有 vs 趨勢規則（收盤在 50 日線上且 50 日線在 200 日線上才持有，隔天進出）。"""
    pairs = LEV.get(key, [])
    syms = [u for u, _, _ in pairs] + [s for _, ls, _ in pairs for s, _ in ls]
    d = yf_download(syms, period="10y", interval="1d")
    out = []
    fin = 0.045 / 252   # 借錢成本（概算）
    fee = 0.0095 / 252  # 槓桿 ETF 管理費
    for u, ls, label in pairs:
        if u not in d:
            continue
        c = (d[u]["Adj Close"] if "Adj Close" in d[u] else d[u]["Close"]).dropna()
        r = c.pct_change(fill_method=None).fillna(0)
        sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
        on = ((c > sma50) & (sma50 > sma200)).shift(1, fill_value=False).astype(float)
        sw = on.diff().abs().fillna(0)
        rows = []
        for lev in (1, 2, 3):
            if key == "tw" and lev == 3:
                continue
            lr = lev * r - (lev - 1) * fin - (fee if lev > 1 else 0)
            for name, rr in (("買進持有", lr), ("趨勢規則", lr * on - sw * COST[key])):
                eq = (1 + rr.iloc[200:]).cumprod()
                five = _stats(eq.iloc[-252 * 5:])
                one = _stats(eq.iloc[-252:])
                rows.append({"lev": lev, "how": name, "y5": five, "y1": one})
        real = []
        for s, lev in ls:
            if s in d:
                rc = (d[s]["Adj Close"] if "Adj Close" in d[s] else d[s]["Close"]).dropna()
                real.append({"sym": s.replace(".TW", ""), "lev": lev, "y1": _stats(rc.iloc[-252:]),
                             "since": str(rc.index[0].date())})
        out.append({"under": u.replace(".TW", ""), "label": label, "rows": rows, "real": real,
                    "in_now": bool(on.iloc[-1]) if len(on) else None,
                    "years": round(len(c.iloc[200:]) / 252, 1)})
    return out


# ───────── 主程式 ─────────

def run(key, cfg, yf_download, now_utc, in_session):
    """scan.py 每次都會呼叫；這裡自己決定要不要做事。"""
    global _probe
    _probe = fu._load(PROBE, {})
    try:
        _run(key, cfg, yf_download, now_utc, in_session)
    except Exception:
        _probe[f"{key}_error"] = traceback.format_exc()[-600:]
    _probe["_updated"] = _now_iso(now_utc)
    try:
        fu._save(PROBE, _probe, compact=False)
    except OSError:
        pass


def _run(key, cfg, yf_download, now_utc, in_session):
    if in_session:
        return
    out_path = DATA_DIR / "rotation" / f"{key}.json"
    uni = universe(key, now_utc)
    if not uni:
        _probe[f"{key}_universe"] = "empty"
        return
    sym = (lambda c: uni[c]["sym"]) if key == "tw" else (lambda c: c)
    bench_sym = cfg["benchmark"] if key == "us" else cfg["benchmark"] + ".TW"
    syms = [bench_sym] + [sym(c) for c in uni]
    close, dv, changed, pending = update_prices(key, syms, yf_download, now_utc)
    _probe[f"{key}_prices"] = {"have": int(close.shape[1]), "pending": pending, "rows": int(close.shape[0])}
    old = fu._load(out_path, {})
    # 台股三大法人個股買賣超：每輪補抓幾天，抓滿一年才拿來回測
    inst_days, inst_ready = {}, True
    if key == "tw" and close.shape[0] >= 80:
        try:
            inst_days = flows.update(CACHE / key, list(close.index[-250:]), probe=_probe)
        except Exception:
            _probe["tw_inst_error"] = traceback.format_exc()[-300:]
        inst_ready = len(inst_days) >= 230
        changed = changed or (_probe.get("tw_inst") or {}).get("new", 0) > 0
    need_bt = "16_grp" not in ((old.get("bt") or {}).get("counts") or {}) or \
        (key == "tw" and inst_ready and "16_inst" not in ((old.get("bt") or {}).get("counts") or {}))
    if not changed and old.get("generated_at") and old.get("pending") == pending and "dates" in old and not need_bt:
        return
    if bench_sym not in close.columns or close.shape[0] < 80:
        return
    back = {sym(c): c for c in uni}
    bench = close[bench_sym]
    close = close.drop(columns=[bench_sym]).rename(columns=back)
    dv = dv.drop(columns=[bench_sym], errors="ignore").rename(columns=back)
    # 流動性：20 日平均成交金額、股價門檻
    liq = (dv.iloc[-20:].mean() >= MIN_DV[key]) & (close.iloc[-1] >= MIN_PX[key])
    tks = [t for t in close.columns if liq.get(t, False)]
    close, dv = close[tks], dv[tks]
    names = {**{c: u["name"] for c, u in uni.items()}, **cfg.get("names", {})}
    members = {"ind": {}, "theme": {}}
    for t in tks:
        members["ind"].setdefault(uni[t]["ind"], []).append(t)
    themes = US_THEMES if key == "us" else TW_THEMES  # 設定檔的主題和這裡重疊，不另外合併，避免同一個題材出現兩次
    members["theme"] = {g: [t for t in v if t in close.columns] for g, v in themes.items()}
    # 族群走勢：成員每天漲跌幅的平均（等權重），近一年從 100 起算
    rets = close.pct_change(fill_method=None).iloc[-251:]
    days = [d.date().isoformat() for d in close.index[-252:]]

    def gidx(tks):
        r = rets[tks].clip(-0.5, 1.0).mean(axis=1).fillna(0)
        v = 100 * np.concatenate([[1.0], (1 + r).cumprod().to_numpy()])
        return [round(float(x), 2) for x in v[-len(days):]]

    b = bench.reindex(close.index).ffill().iloc[-252:]
    bench_idx = [round(float(x / b.iloc[0] * 100), 2) for x in b]
    res = {}
    for kind, mem in members.items():
        now_t = group_table(close, dv, bench, mem)
        prev = group_table(close, dv, bench, mem, at=-6)  # 一週前
        rows = []
        for g, r in sorted(now_t.items(), key=lambda kv: kv[1]["rank"]):
            p = prev.get(g)
            ld = leaders(close, r["tks"])
            rows.append({"name": g, "n": r["n"], "score": round(r["score"]), "rank": r["rank"],
                         "rank_prev": p["rank"] if p else None,
                         "r5": round(r["r5"] * 100, 1), "r21": round(r["r21"] * 100, 1), "r63": round(r["r63"] * 100, 1),
                         "breadth": round(r["breadth"] * 100), "flow": round(r["flow"] * 100), "heat": round(r["heat"], 2),
                         "leaders": [{"t": t, "name": names.get(t, ""),
                                      "r21": round(float(close[t].iloc[-1] / close[t].iloc[-22] - 1) * 100, 1)} for t in ld],
                         "idx": gidx(r["tks"])})
        res[kind] = rows
    out = {"market": key, "generated_at": _now_iso(now_utc), "data_date": str(close.index[-1].date()),
           "n_stocks": len(tks), "pending": pending, "groups": res["ind"], "themes": res["theme"],
           "bench": cfg["benchmark"], "dates": days, "bench_idx": bench_idx,
           "notes": {"score": "族群分數 0～100：近 1 月超額報酬 35%、近 3 月 30%、近 1 週 10%、資金淨流入 15%、站上 50 日線比例 10%（都是和其他族群比的排名）",
                     "flow": "資金淨流入：近 20 天上漲日成交金額減下跌日，占總成交金額的百分比",
                     "heat": "成交熱度：近 20 天成交金額 ÷ 近 60 天平均，再和全市場比；大於 1 代表資金在加碼這個族群",
                     "excess": "報酬都是族群中位數減大盤（" + cfg["benchmark"] + "）",
                     "universe": f"{'市值 20 億美元以上' if key == 'us' else '上市櫃'}、近 20 天平均成交金額 {'2,000 萬美元' if key == 'us' else '5,000 萬台幣'}以上的股票"}}
    if pending:
        out["notes"]["pending"] = f"還有 {pending} 檔在下載，族群資料會越來越完整"
    # 回測：一天一次
    if old.get("bt") and old.get("bt_date") == out["data_date"] and not need_bt:
        out["bt"], out["bt_date"], out["lev"] = old["bt"], old["bt_date"], old.get("lev")
    elif not pending:
        try:
            out["bt"] = {k: rotation_backtest(close, dv, bench, m, key) for k, m in members.items()}
            inst = None
            if key == "tw" and inst_ready:
                nv = flows.net_value(inst_days, close, list(close.columns))
                inst = nv.rolling(20, min_periods=15).sum() / dv.rolling(20).sum()
            out["bt"]["counts"] = count_backtest(close, dv, bench, key, groups=members["ind"], inst=inst)
            out["bt_date"] = out["data_date"]
            out["bt_note"] = "名單是現在的股票，沒有包含過去下市或掉出門檻的股票（倖存者偏差），實際會比回測差一些。"
        except Exception:
            _probe[f"{key}_bt"] = traceback.format_exc()[-400:]
        try:
            out["lev"] = lev_backtest(key, yf_download)
        except Exception:
            _probe[f"{key}_lev"] = traceback.format_exc()[-400:]
    fu._save(out_path, out)
