"""线上版多源采集层 —— 四层降级，任何一层失败都不至于空页。

  L0  a-stock-api Worker   /api/fund-nav、/api/etf-premium、/api/trade-calendar
  L1  东方财富 / 腾讯 / 新浪 直连   不依赖任何第三方抓取库，字段契约实测固定
  L2  AKShare              批量净值表、申购状态、份额规模、集思录交叉校验
  L3  仓库内 seed.json / state.json  上一次成功快照与历史序列

已核实的坑（不要在别处重复踩）：
  * Worker 的 /api/global-index 只支持 KOSPI / 恒生 / 富时100，**不支持标普500 / 纳指100**，
    所以美股基准走东财 100.SPX / 100.NDX100 日线（与新浪 .INX / .NDX 数值逐笔一致）。
  * 东财 100.NDX 是「纳斯达克综合」，100.NDX100 才是纳斯达克100，别用错。
  * /api/etf-premium 单批最多 30 个代码，且 stale=true 时只是「相对昨日净值的偏离」。
  * /api/etf-shares 的 data:[] 不代表没数据，要看 upstream_error。
  * stooq 现在有 JS 校验页，不能作为兜底。
  * pingzhongdata 的 x 是北京零点时间戳（UTC-16:00），必须 +8h 再取日期。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
log = logging.getLogger("qdii.sources")

DT = "datetime64[ns]"
TZ_BJ = dt.timezone(dt.timedelta(hours=CFG.get("timezone_offset_hours", 8)))

EM_SPOT_HOSTS = [
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://88.push2.eastmoney.com/api/qt/clist/get",
]
EM_KLINE_HOSTS = [
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://33.push2his.eastmoney.com/api/qt/stock/kline/get",
]
EM_ETF_FS = "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0827"
EM_LOF_FS = "b:MK0404,b:MK0405,b:MK0406,b:MK0407"
EM_SPOT_FIELDS = "f2,f6,f12,f14,f20,f21,f30,f31,f32,f38,f402,f441,f297,f124"

SPOT_MAP = {
    "f12": "代码", "f14": "名称", "f2": "最新价", "f441": "IOPV实时估值",
    "f402": "基金折价率", "f6": "成交额", "f21": "流通市值", "f20": "总市值",
    "f38": "最新份额", "f31": "买一", "f32": "卖一", "f124": "更新时间",
}

STATUS: dict = {
    "generated_at": "",
    "chains": {},
    "warnings": [],
    "worker_reachable": None,
    "crosscheck": {},
}


def bj_now() -> dt.datetime:
    return dt.datetime.now(TZ_BJ)


def bj_today() -> str:
    return bj_now().date().isoformat()


def _to_dt(s):
    return pd.Timestamp(s).as_unit("ns")


def _nonempty(v) -> bool:
    if v is None:
        return False
    if isinstance(v, pd.DataFrame):
        return not v.empty
    if isinstance(v, (dict, list, tuple, set, pd.Series)):
        return len(v) > 0
    return bool(v)


# 辅助链路：失败时已有等价替代（季报净资产 / 直连价量 / 其它校验源），
# 不计入「核心降级」，但仍完整展示在数据源面板里。
OPTIONAL_CHAINS = {"场外份额规模", "集思录交叉校验", "Worker 溢价交叉校验"}


def chain(name: str, providers: list, validator=None, optional: bool = False):
    """依次尝试 provider，返回第一个通过校验的值，并把整条降级链记进 STATUS。"""
    attempts = []
    for label, fn in providers:
        t0 = time.time()
        try:
            val = fn()
            ok = bool((validator or _nonempty)(val))
            attempts.append({"source": label, "ok": ok,
                             "ms": int((time.time() - t0) * 1000),
                             "detail": _describe(val)})
            if ok:
                STATUS["chains"][name] = {"active": label, "attempts": attempts,
                                          "failed": False, "optional": optional}
                log.info("[%s] 命中 %s (%s)", name, label, _describe(val))
                return val
        except Exception as exc:  # noqa: BLE001
            attempts.append({"source": label, "ok": False,
                             "ms": int((time.time() - t0) * 1000),
                             "detail": f"{type(exc).__name__}: {str(exc)[:140]}"})
            log.info("[%s] %s 失败: %s", name, label, str(exc)[:140])
    STATUS["chains"][name] = {"active": "none", "attempts": attempts,
                              "failed": True, "optional": optional}
    STATUS["warnings"].append(
        f"{name}：辅助源全部不可用，已有等价替代源覆盖，不影响结果" if optional
        else f"{name}：所有数据源均不可用，已降级")
    return None


def _describe(v) -> str:
    if isinstance(v, pd.DataFrame):
        return f"{len(v)} 行"
    if isinstance(v, dict):
        return f"{len(v)} 项"
    if isinstance(v, list):
        return f"{len(v)} 条"
    return str(v)[:80]


# --------------------------------------------------------------------------
# HTTP 基础工具
# --------------------------------------------------------------------------

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"}


def http_get(urls, params=None, timeout: int = 0, retries: int | None = None,
             headers=None, encoding: str | None = None):
    """多 host 轮换 + 重试。urls 可以是字符串或字符串列表。"""
    timeout = timeout or CFG.get("http_timeout_sec", 20)
    retries = CFG.get("http_retries", 1) if retries is None else retries
    url_list = [urls] if isinstance(urls, str) else list(urls)
    h = dict(UA)
    if headers:
        h.update(headers)
    last = None
    for url in url_list:
        for attempt in range(retries + 1):
            try:
                r = requests.get(url, params=params, headers=h, timeout=timeout)
                r.raise_for_status()
                if encoding:
                    r.encoding = encoding
                return r
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < retries:
                    time.sleep(0.5)
    raise last if last else RuntimeError("no url")


def http_json(urls, params=None, **kw):
    return http_get(urls, params=params, **kw).json()


def safe_call(fn, *args, timeout: int = 60, retries: int = 1, **kwargs):
    """AKShare 内部请求不带 timeout，用单线程 Future 包一层；超时不阻塞。"""
    last = None
    for attempt in range(retries + 1):
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(fn, *args, **kwargs).result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.4)
        finally:
            pool.shutdown(wait=False)
    raise last


def _ak():
    import akshare as ak
    return ak


# --------------------------------------------------------------------------
# L0：a-stock-api Worker
# --------------------------------------------------------------------------

WORKER = str(CFG.get("worker_base", "")).rstrip("/")


def worker_enabled() -> bool:
    return bool(CFG.get("worker_enabled", True) and WORKER)


def worker_get(path: str, params: dict | None = None):
    if not worker_enabled():
        raise RuntimeError("worker disabled")
    r = http_get(WORKER + path, params=params,
                 timeout=CFG.get("worker_timeout_sec", 20), retries=0)
    j = r.json()
    if not isinstance(j, dict) or j.get("error"):
        raise RuntimeError(str(j.get("error") or "bad worker payload")[:120])
    return j


def worker_health() -> dict:
    try:
        worker_get("/api/trade-calendar", {"start": bj_today(), "end": bj_today()})
        STATUS["worker_reachable"] = True
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        STATUS["worker_reachable"] = False
        STATUS["warnings"].append(f"Worker 不可达：{type(exc).__name__}（自动走直连源）")
        return {"ok": False}


def worker_fund_nav(code: str, start: str = "", end: str = "") -> pd.DataFrame:
    """契约：{code,count,data:[{date,nav,acc_nav,equity_return}]}。"""
    j = worker_get("/api/fund-nav", {"code": code, "start": start, "end": end})
    rows = j.get("data") or []
    if not rows:
        raise RuntimeError("worker fund-nav 空结果")
    out = pd.DataFrame([
        {"date": _to_dt(x["date"]),
         "nav": pd.to_numeric(x.get("nav"), errors="coerce"),
         "cum_nav": pd.to_numeric(x.get("acc_nav"), errors="coerce")}
        for x in rows if x.get("date") and x.get("nav") is not None
    ])
    out.loc[out["cum_nav"].isna() | (out["cum_nav"] <= 0), "cum_nav"] = np.nan
    out["cum_nav"] = out["cum_nav"].fillna(out["nav"])
    out["nav"] = out["cum_nav"]
    out["date"] = out["date"].astype(DT)
    return out.dropna(subset=["date", "nav"]).query("nav > 0").sort_values("date").reset_index(drop=True)


def worker_etf_premium(codes: list) -> dict:
    """契约：{count,data:[{code,name,price,nav,price_date,nav_date,premium_pct,stale}]}。
    单批最多 30 个代码；空结果要结合 upstream_error 判断。"""
    out = {}
    size = int(CFG.get("worker_batch_size", 30))
    for i in range(0, len(codes), size):
        batch = codes[i:i + size]
        try:
            j = worker_get("/api/etf-premium", {"codes": ",".join(batch)})
            for row in j.get("data") or []:
                out[str(row.get("code", "")).zfill(6)] = row
            if j.get("upstream_error"):
                STATUS["warnings"].append(f"Worker etf-premium 部分失败：{str(j['upstream_error'])[:120]}")
        except Exception as exc:  # noqa: BLE001
            log.debug("worker premium 批次失败: %s", exc)
    if not out:
        raise RuntimeError("worker etf-premium 无有效结果")
    return out


# --------------------------------------------------------------------------
# L1：直连源
# --------------------------------------------------------------------------

def direct_fund_names() -> pd.DataFrame:
    """天天基金全量名录 JS（3.2MB，极稳定，不依赖 akshare）。"""
    r = http_get("https://fund.eastmoney.com/js/fundcode_search.js", timeout=35,
                 headers={"Referer": "https://fund.eastmoney.com/"})
    t = r.text
    arr = json.loads(t[t.index("["):t.rindex("]") + 1])
    df = pd.DataFrame(
        [{"基金代码": str(x[0]).zfill(6), "基金简称": x[2], "基金类型": x[3]} for x in arr]
    )
    if df.empty:
        raise RuntimeError("fundcode_search 解析为空")
    return df


def ak_fund_names() -> pd.DataFrame:
    df = safe_call(_ak().fund_name_em, timeout=90)
    df["基金代码"] = df["基金代码"].astype(str).str.zfill(6)
    return df


def _em_spot(fs: str) -> pd.DataFrame:
    """东财 clist 分页；三个 host 轮换，任一可用即完整返回。"""
    frames, page, total = [], 1, None
    while page <= 30:
        params = {
            "pn": page, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f12", "fs": fs, "fields": EM_SPOT_FIELDS,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        }
        j = http_json(EM_SPOT_HOSTS, params=params, timeout=20)
        data = j.get("data") or {}
        diff = data.get("diff") or []
        total = data.get("total") or total
        if not diff:
            break
        frames.extend(diff)
        if total and len(frames) >= int(total):
            break
        page += 1
    if not frames:
        raise RuntimeError("clist 无数据")
    df = pd.DataFrame(frames).rename(columns=SPOT_MAP)
    for c in ("最新价", "IOPV实时估值", "基金折价率", "成交额", "流通市值",
              "总市值", "最新份额", "买一", "卖一"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = np.nan
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    # f297 实测是 YYYYMMDD 整数（如 20261110），不是 unix 时间戳；
    # f124 才是秒级更新时间戳。曾误按秒解析得到 1970-08-23，导致估算溢价全部降级。
    ts = pd.to_numeric(df.get("f297"), errors="coerce").fillna(0).astype("int64").astype(str)
    yyyymmdd = ts.str.fullmatch(r"20\d{6}")
    df["数据日期"] = np.where(
        yyyymmdd, ts.str[:4] + "-" + ts.str[4:6] + "-" + ts.str[6:8], bj_today())
    df["数据日期"] = pd.Series(df["数据日期"]).fillna(bj_today()).values
    return df[["代码", "名称", "最新价", "IOPV实时估值", "基金折价率", "成交额",
               "流通市值", "总市值", "最新份额", "买一", "卖一", "数据日期"]]


def direct_etf_spot() -> pd.DataFrame:
    return _em_spot(EM_ETF_FS)


def direct_lof_spot() -> pd.DataFrame:
    return _em_spot(EM_LOF_FS)


def ak_etf_spot() -> pd.DataFrame:
    df = safe_call(_ak().fund_etf_spot_em, timeout=90)
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    return df


def ak_lof_spot() -> pd.DataFrame:
    df = safe_call(_ak().fund_lof_spot_em, timeout=90)
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    return df


def _tencent_prefix(code: str) -> str:
    return "sh" if code.startswith("5") else "sz"


def direct_tencent_spot(codes: list) -> pd.DataFrame:
    """腾讯行情批量快照（一份请求可带几十个代码），作为东财整体被墙时的价格兜底。
    字段：3=现价 9=买一 19=卖一 30=时间戳 37=成交额(万元)。"""
    codes = [str(c).zfill(6) for c in codes]
    frames = []
    for i in range(0, len(codes), 60):
        batch = codes[i:i + 60]
        r = http_get("https://qt.gtimg.cn/q=" + ",".join(_tencent_prefix(c) + c for c in batch),
                     timeout=20, encoding="gbk")
        for line in r.text.split(";"):
            m = re.search(r'v_(?:sh|sz)(\d{6})="(.*?)"', line)
            if not m:
                continue
            code, f = m.group(1), m.group(2).split("~")
            if len(f) < 38:
                continue
            def num(idx):
                try:
                    return float(f[idx])
                except (ValueError, IndexError):
                    return np.nan
            t = f[30] if len(f[30]) >= 8 else ""
            frames.append({
                "代码": code, "名称": f[1], "最新价": num(3),
                "IOPV实时估值": np.nan, "基金折价率": np.nan,
                "成交额": num(37) * 1e4 if np.isfinite(num(37)) else np.nan,
                "流通市值": np.nan, "总市值": np.nan, "最新份额": np.nan,
                "买一": num(9), "卖一": num(19),
                "数据日期": f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) >= 8 else bj_today(),
            })
    if not frames:
        raise RuntimeError("腾讯行情解析为空")
    return pd.DataFrame(frames)


def _pz_text(code: str) -> str:
    return http_get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js", timeout=20,
                    headers={"Referer": f"https://fund.eastmoney.com/{code}.html"}).text


def direct_pz_nav(code: str) -> pd.DataFrame:
    """pingzhongdata 全量净值走势（与 Worker /api/fund-nav 同源，但直连不经过 Worker）。"""
    t = _pz_text(code)
    m = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", t, re.S)
    if not m:
        raise RuntimeError("无 Data_netWorthTrend")
    arr = json.loads(m.group(1))
    acc = {}
    ma = re.search(r"var\s+Data_ACWorthTrend\s*=\s*(\[.*?\]);", t, re.S)
    if ma:
        try:
            for x in json.loads(ma.group(1)):
                if isinstance(x, list) and len(x) >= 2:
                    acc[x[0]] = x[1]
        except Exception:  # noqa: BLE001
            pass
    rows = []
    for x in arr:
        if not isinstance(x, dict) or x.get("x") is None or x.get("y") is None:
            continue
        d = (dt.datetime.fromtimestamp(float(x["x"]) / 1000, tz=dt.timezone.utc)
             + dt.timedelta(hours=8)).date().isoformat()
        nav = pd.to_numeric(x.get("y"), errors="coerce")
        cum = pd.to_numeric(acc.get(x["x"]), errors="coerce")
        if pd.isna(nav) or nav <= 0:
            continue
        rows.append((d, float(cum if pd.notna(cum) and cum > 0 else nav)))
    if not rows:
        raise RuntimeError("净值序列为空")
    df = pd.DataFrame(rows, columns=["date", "nav"])
    df["date"] = pd.to_datetime(df["date"].map(_to_dt)).astype(DT)
    return df.sort_values("date").reset_index(drop=True)


def direct_pz_scale(code: str):
    t = _pz_text(code)
    m = re.search(r"var\s+Data_fluctuationScale\s*=\s*(\{.*?\});", t, re.S)
    if not m:
        return None
    obj = json.loads(m.group(1))
    cats, series = obj.get("categories") or [], obj.get("series") or []
    if cats and series:
        val = float(series[-1].get("y"))
        if val > 0:
            return {"net_asset_yi": val, "scale_date": str(cats[-1])}
    return None


def direct_us_index(key: str) -> pd.DataFrame:
    """东财 100.SPX / 100.NDX100 日线，与新浪 .INX / .NDX 数值逐笔一致。"""
    secid = CFG["index_secids_em"][key]
    j = http_json(EM_KLINE_HOSTS, params={
        "secid": secid, "fields1": "f1,f2,f3", "fields2": "f51,f53",
        "klt": "101", "fqt": "0", "beg": "20200101", "end": "20500101",
    })
    klines = ((j.get("data") or {}).get("klines") or [])
    if not klines:
        raise RuntimeError(f"{secid} 无 K 线")
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) >= 2:
            rows.append((p[0], float(p[1])))
    df = pd.DataFrame(rows, columns=["date", "close"])
    df["date"] = pd.to_datetime(df["date"].map(_to_dt)).astype(DT)
    return df.dropna().sort_values("date").reset_index(drop=True)


def ak_us_index(key: str) -> pd.DataFrame:
    sym = CFG["index_symbols_sina"][key]
    df = safe_call(_ak().index_us_stock_sina, symbol=sym, timeout=90)
    out = df[["date", "close"]].copy()
    out["date"] = pd.to_datetime(out["date"].map(_to_dt)).astype(DT)
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.dropna().sort_values("date").reset_index(drop=True)


def direct_fx() -> pd.DataFrame:
    j = http_json(EM_KLINE_HOSTS, params={
        "secid": "133.USDCNH", "fields1": "f1,f2,f3", "fields2": "f51,f53",
        "klt": "101", "fqt": "0", "beg": "20200101", "end": "20500101",
    })
    klines = ((j.get("data") or {}).get("klines") or [])
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) >= 2:
            rows.append((p[0], float(p[1])))
    if not rows:
        raise RuntimeError("汇率 K 线为空")
    df = pd.DataFrame(rows, columns=["date", "close"])
    df["date"] = pd.to_datetime(df["date"].map(_to_dt)).astype(DT)
    return df.dropna().sort_values("date").reset_index(drop=True)


def direct_price_history(code: str) -> pd.DataFrame:
    market = "1" if code.startswith("5") else "0"
    j = http_json(EM_KLINE_HOSTS, params={
        "secid": f"{market}.{code}", "fields1": "f1,f2,f3", "fields2": "f51,f53",
        "klt": "101", "fqt": "0", "beg": "20200101", "end": "20500101",
    })
    klines = ((j.get("data") or {}).get("klines") or [])
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) >= 2:
            rows.append((p[0], float(p[1])))
    if not rows:
        raise RuntimeError("价格 K 线为空")
    df = pd.DataFrame(rows, columns=["date", "close"])
    df["date"] = pd.to_datetime(df["date"].map(_to_dt)).astype(DT)
    return df.dropna().sort_values("date").reset_index(drop=True)


def ak_price_history(code: str) -> pd.DataFrame:
    df = safe_call(_ak().fund_etf_hist_em, symbol=code, period="daily",
                   start_date="20200101", end_date=bj_today().replace("-", ""),
                   adjust="", timeout=45)
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["日期"].map(_to_dt)).astype(DT)
    out["close"] = pd.to_numeric(df["收盘"], errors="coerce")
    return out.dropna().sort_values("date").reset_index(drop=True)


# --------------------------------------------------------------------------
# L2：AKShare 批量源
# --------------------------------------------------------------------------

def _daily_nav_cols(df: pd.DataFrame):
    dates = []
    for col in df.columns:
        if str(col).endswith("-单位净值"):
            dates.append(str(col)[:-len("-单位净值")])
    return sorted(set(dates), reverse=True)


def parse_daily_nav(df: pd.DataFrame) -> dict:
    """东财全量净值表：列名是动态日期，QDII 当日列常为 '---'，倒序取首个有效值。"""
    out = {}
    dates = _daily_nav_cols(df)
    if "基金代码" not in df.columns:
        return out
    for _, row in df.iterrows():
        code = str(row["基金代码"]).zfill(6)
        for d in dates:
            val = pd.to_numeric(row.get(f"{d}-单位净值"), errors="coerce")
            if pd.notna(val) and val > 0:
                cum = pd.to_numeric(row.get(f"{d}-累计净值"), errors="coerce")
                out[code] = {"nav": float(val), "nav_date": d,
                             "cum_nav": float(cum) if pd.notna(cum) and cum > 0 else None}
                break
    return out


def ak_daily_nav() -> dict:
    out = {}
    for label, fn in (("open", _ak().fund_open_fund_daily_em),
                      ("etf", _ak().fund_etf_fund_daily_em)):
        try:
            out.update(parse_daily_nav(safe_call(fn, timeout=120)))
        except Exception as exc:  # noqa: BLE001
            log.debug("ak 净值表 %s 失败: %s", label, exc)
    if not out:
        raise RuntimeError("ak 净值表全失败")
    return out


def ak_nav_history(code: str, venue: str) -> pd.DataFrame:
    ak = _ak()
    if venue == "ETF":
        df = safe_call(ak.fund_etf_fund_info_em, fund=code, timeout=40)
    else:
        df = safe_call(ak.fund_open_fund_info_em, symbol=code,
                       indicator="单位净值走势", timeout=40)
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["净值日期"].map(_to_dt)).astype(DT)
    out["nav"] = pd.to_numeric(df["单位净值"], errors="coerce")
    if "累计净值" in df.columns:
        out["cum_nav"] = pd.to_numeric(df["累计净值"], errors="coerce")
        bad = out["cum_nav"].isna() | (out["cum_nav"] <= 0)
        out.loc[bad, "cum_nav"] = out.loc[bad, "nav"]
        out["nav"] = out["cum_nav"]
    return out.dropna(subset=["date", "nav"]).query("nav > 0").sort_values("date").reset_index(drop=True)


def ak_purchase() -> dict:
    df = safe_call(_ak().fund_purchase_em, timeout=180)
    out = {}
    for _, row in df.iterrows():
        code = str(row.get("基金代码", "")).zfill(6)
        if code == "000000":
            continue
        limit = pd.to_numeric(row.get("日累计限定金额"), errors="coerce")
        out[code] = {
            "purchase_status": str(row.get("申购状态", "") or ""),
            "redeem_status": str(row.get("赎回状态", "") or ""),
            "limit_amount": float(limit) if pd.notna(limit) else None,
            "fee": pd.to_numeric(row.get("手续费"), errors="coerce"),
        }
    return out


def ak_scale_open() -> dict:
    """AKShare 版本会随上游字段增减直接抛 KeyError，这里只作为可选源之一。"""
    df = safe_call(_ak().fund_scale_open_sina, symbol="QDII基金", timeout=90)
    return _parse_sina_scale(df)


SINA_SCALE_URL = ("http://vip.stock.finance.sina.com.cn/fund_center/data/jsonp.php/"
                  "IO.XSRV2.CallbackList['J2cW8KXheoWKdSHc']/"
                  "NetValueReturn_Service.NetValueReturnOpen")


def direct_sina_scale() -> dict:
    """直连新浪原始 JSON（symbol / zjzfe / dwjz / jzrq），绕开 AKShare 的固定列选择。"""
    r = http_get(SINA_SCALE_URL, params={"page": 1, "num": 10000, "sort": "zmjgm",
                                         "asc": 0, "ccode": "", "type2": 6, "type3": ""},
                 timeout=25, headers={"Referer": "https://vip.stock.finance.sina.com.cn/"})
    t = r.text
    a, b = t.find("({"), t.rfind("})")
    if a < 0 or b < 0:
        raise RuntimeError("新浪份额 JSONP 格式异常")
    obj = json.loads(t[a + 1:b + 1])
    data = obj.get("data") or []
    if not data:
        raise RuntimeError("新浪 QDII 份额上游返回空（常见，季报净资产会自动兜底）")
    df = pd.DataFrame(data)
    return _parse_sina_scale(df)


def _parse_sina_scale(df: pd.DataFrame) -> dict:
    out = {}
    code_col = "基金代码" if "基金代码" in df.columns else ("symbol" if "symbol" in df.columns else None)
    if not code_col:
        raise RuntimeError("新浪份额表无代码列")
    share_col = "最近总份额" if "最近总份额" in df.columns else "zjzfe"
    nav_col = "单位净值" if "单位净值" in df.columns else "dwjz"
    date_col = "更新日期" if "更新日期" in df.columns else "jzrq"
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).zfill(6)
        shares = pd.to_numeric(row.get(share_col), errors="coerce")
        nav = pd.to_numeric(row.get(nav_col), errors="coerce")
        if pd.isna(shares) or shares <= 0:
            continue
        out[code] = {"shares": float(shares),
                     "scale_nav": float(nav) if pd.notna(nav) and nav > 0 else None,
                     "scale_date": str(row.get(date_col, "") or "")}
    if not out:
        raise RuntimeError("新浪份额表无有效行")
    return out


def ak_jisilu() -> dict:
    df = safe_call(_ak().qdii_e_index_jsl, timeout=60)
    out = {}
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).zfill(6)
        t2 = pd.to_numeric(row.get("T-2净值"), errors="coerce")
        out[code] = {"t2_nav": float(t2) if pd.notna(t2) else None,
                     "nav_date": str(row.get("净值日期", "") or ""),
                     "related": str(row.get("相关标的", "") or ""),
                     "jsl_premium": str(row.get("T-1溢价率", "") or "")}
    return out


# --------------------------------------------------------------------------
# 历史序列：逐代码静默降级（不把 94 条链写进状态文件）
# --------------------------------------------------------------------------

QUIET_HITS: dict = {}


def quiet_chain(providers: list, validator, counter: str):
    for label, fn in providers:
        try:
            val = fn()
            if validator(val):
                QUIET_HITS.setdefault(counter, {}).setdefault(label, 0)
                QUIET_HITS[counter][label] += 1
                return val, label
        except Exception:  # noqa: BLE001
            continue
    QUIET_HITS.setdefault(counter, {}).setdefault("cached_or_failed", 0)
    QUIET_HITS[counter]["cached_or_failed"] += 1
    return None, "cached_or_failed"


def nav_history(code: str, venue: str, cached: pd.DataFrame | None = None) -> pd.DataFrame:
    providers = []
    if worker_enabled():
        providers.append(("L0 Worker fund-nav", lambda: worker_fund_nav(code)))
    providers += [
        ("L1 pingzhongdata 直连", lambda: direct_pz_nav(code)),
        ("L2 AKShare 净值走势", lambda: ak_nav_history(code, venue)),
    ]
    val, _ = quiet_chain(providers, lambda v: v is not None and len(v) >= 30, "nav_history")
    if val is not None:
        return val
    return cached if cached is not None and not cached.empty else pd.DataFrame(columns=["date", "nav"])


def price_history(code: str, cached: pd.DataFrame | None = None) -> pd.DataFrame:
    providers = [
        ("L1 东财 K 线直连", lambda: direct_price_history(code)),
        ("L2 AKShare ETF 历史", lambda: ak_price_history(code)),
    ]
    val, _ = quiet_chain(providers, lambda v: v is not None and len(v) >= 30, "price_history")
    if val is not None:
        return val
    return cached if cached is not None and not cached.empty else pd.DataFrame(columns=["date", "close"])


def worker_crosscheck(codes: list) -> dict:
    """Worker /api/etf-premium 作为「第二意见」：交叉校验价格/净值，缺数据时补位。"""
    if not worker_enabled() or not codes:
        return {}
    try:
        out = chain("Worker 溢价交叉校验",
                    [("L0 Worker etf-premium", lambda: worker_etf_premium(codes))],
                    optional=True)
        STATUS["crosscheck"]["worker_premium_rows"] = len(out or {})
        return out or {}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------
# 主采集：把每条链路的命中情况写进 STATUS，供页面「数据源健康」面板展示
# --------------------------------------------------------------------------

CHAIN_LABELS = {
    "基金名录": [("L1 天天基金名录 JS", direct_fund_names, lambda v: v is not None and len(v) > 5000),
              ("L2 AKShare fund_name_em", ak_fund_names, lambda v: v is not None and len(v) > 5000)],
    "ETF 行情": [("L1 东财 clist 直连", direct_etf_spot, lambda v: v is not None and len(v) > 100),
              ("L2 AKShare fund_etf_spot_em", ak_etf_spot, lambda v: v is not None and len(v) > 100)],
    "LOF 行情": [("L1 东财 clist 直连", direct_lof_spot, lambda v: v is not None and len(v) > 10),
              ("L2 AKShare fund_lof_spot_em", ak_lof_spot, lambda v: v is not None and len(v) > 10)],
    "最新净值批量表": [("L2 AKShare 全量净值表", ak_daily_nav, lambda v: bool(v) and len(v) > 1000)],
    "申购状态": [("L2 AKShare fund_purchase_em", ak_purchase, _nonempty)],
    "场外份额规模": [("L2 AKShare 新浪 QDII 份额", ak_scale_open, _nonempty),
                ("L1 新浪份额 JSONP 直连", direct_sina_scale, _nonempty)],
    "集思录交叉校验": [("L2 AKShare 集思录 QDII", ak_jisilu, _nonempty)],
    "汇率 USD/CNH": [("L1 东财 K 线直连", direct_fx, lambda v: v is not None and len(v) > 100)],
}


def collect(seed: dict, fallback_codes: list | None = None) -> dict:
    """返回组装 data 所需的全部原始素材；任何一条链失败都只记录、不抛出。"""
    STATUS["generated_at"] = bj_now().isoformat(timespec="seconds")
    STATUS["chains"] = {}
    STATUS["warnings"] = []
    STATUS["crosscheck"] = {}
    QUIET_HITS.clear()
    worker_health()

    raw = {}
    for name, providers in CHAIN_LABELS.items():
        raw[name] = chain(name, [(lbl, fn) for lbl, fn, _ in providers],
                          validator=providers[0][2], optional=name in OPTIONAL_CHAINS)

    # 美股指数：逐指数独立降级，L1 东财 → L2 新浪/AKShare → L3 state 缓存
    raw["index"] = {}
    for key in ("SP500", "NDX100"):
        df = chain(f"美股指数 {key}", [
            ("L1 东财 100.SPX/100.NDX100 日线", lambda k=key: direct_us_index(k)),
            ("L2 新浪指数日线（AKShare）", lambda k=key: ak_us_index(k)),
        ], validator=lambda v: v is not None and len(v) > 100)
        raw["index"][key] = df

    # 腾讯行情作为第三种价格源，只在东财 ETF+LOF 全挂时启用。
    # 关键约束：只看「上次已确认的场内代码」。场外 6 位代码与 A 股股票代码
    # 会重合（000055 = 广发纳指100联接美元份额 = 深市方大集团），一旦按
    # sz000055 请求就会取回股票价，算出 200%+ 的假溢价。请求前和请求后
    # 都做一次白名单交集，双保险。
    fallback_used = False
    if (raw["ETF 行情"] is None or raw["ETF 行情"].empty) and fallback_codes:
        allowed = {str(c).zfill(6) for c in fallback_codes}
        tx = chain("ETF 行情（腾讯兜底）", [
            ("L1 腾讯行情批量快照", lambda: direct_tencent_spot(sorted(allowed)))
        ], validator=lambda v: v is not None and len(v) > 0)
        if tx is not None:
            tx = tx[tx["代码"].astype(str).str.zfill(6).isin(allowed)].copy()
            if tx.empty:
                STATUS["warnings"].append(
                    "腾讯兜底返回的代码均不在已确认场内白名单内，已整体丢弃")
            else:
                raw["ETF 行情"] = tx.reset_index(drop=True)
                fallback_used = True

    empty_spot = pd.DataFrame(columns=["代码", "名称", "最新价", "IOPV实时估值", "基金折价率",
                                       "成交额", "流通市值", "总市值", "最新份额",
                                       "买一", "卖一", "数据日期"])
    empty_names = pd.DataFrame(columns=["基金代码", "基金简称", "基金类型"])
    etf = raw["ETF 行情"] if raw["ETF 行情"] is not None else empty_spot
    lof = raw["LOF 行情"] if raw["LOF 行情"] is not None else empty_spot
    names = raw["基金名录"] if raw["基金名录"] is not None else empty_names

    price_date = bj_today()
    for df in (etf, lof):
        if "数据日期" in df.columns and not df.empty:
            v = str(df["数据日期"].dropna().iloc[0])[:10]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                price_date = v
                break

    fx = raw["汇率 USD/CNH"]
    fx = fx if fx is not None else pd.DataFrame(columns=["date", "close"])
    out = {
        "names": names, "etf_spot": etf, "lof_spot": lof,
        "nav_map": raw["最新净值批量表"] or {},
        "purchase": raw["申购状态"] or {},
        "scale": raw["场外份额规模"] or {},
        "jsl": raw["集思录交叉校验"] or {},
        "index": raw["index"], "fx": fx,
        "fx_latest": float(fx.iloc[-1]["close"]) if not fx.empty else None,
        "price_date": price_date,
        "price_fallback_used": fallback_used,
        "worker_premium": {},
        "seed": seed,
    }
    STATUS["degraded_chains"] = [k for k, v in STATUS["chains"].items()
                                 if v.get("failed") and not v.get("optional")]
    STATUS["optional_degraded"] = [k for k, v in STATUS["chains"].items()
                                   if v.get("failed") and v.get("optional")]
    return out
