"""线上版一键刷新（由 GitHub Actions 每天 21:00 北京时间调用）。

与本地版的区别：
  * 无 Flask / 无 SQLite / 无定时任务，状态全部落在 data/state.json 与 dist/history.json；
  * 所有取数走 sources.collect() 的四层降级链；
  * 全部实时源失效时用上一次成功快照出页，并打上「数据陈旧」横幅，绝不空白页；
  * 产物写入 dist/，由 tools/deploy.sh 以「孤立单提交 + 强推」发布，仓库体积恒定。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

import metrics
import sources

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
DIST = BASE / "dist"
STATE_PATH = BASE / "data" / "state.json"
REGISTRY_PATH = BASE / "data" / "registry.json"
SEED_PATH = BASE / "data" / "seed.json"
TEMPLATE_PATH = BASE / "templates" / "index.html"

log = logging.getLogger("qdii.refresh")

COMPANIES = [
    "华泰柏瑞", "景顺长城", "汇添富", "易方达", "广发", "华夏", "南方", "博时",
    "国泰", "华安", "嘉实", "富国", "招商", "大成", "摩根", "天弘", "建信",
    "万家", "宝盈", "鹏华", "工银瑞信", "平安", "银华", "交银", "浦银安盛",
    "国寿安保", "申万菱信", "中信建投", "创金合信", "诺安", "华宝", "长城",
    "长信", "中金", "兴业", "安信", "光大保德信", "国投瑞银", "永赢", "中欧",
]
USD_WORDS = ("美元现汇", "美元现钞", "美钞", "美汇", "美元")

COLUMNS = [
    "code", "name", "index_key", "venue", "currency", "share_class", "family",
    "primary_code", "price", "nav", "nav_date", "est_nav", "est_ratio",
    "us_nav_session", "us_price_session", "iopv", "static", "estimated", "iopv_premium",
    "primary", "premium_kind", "downgraded", "quality", "doubt", "rejected",
    "scale_yi", "scale_source", "scale_date", "amount_yi", "te", "td", "r2",
    "te_price", "premium_noise", "te_n", "te_status", "purchase_status",
    "limit_amount", "fee", "score", "p_premium", "p_tracking", "p_scale", "p_liquidity",
    "related", "status", "first_seen",
]


# --------------------------------------------------------------------------
# 名单自动发现（与本地版同口径）
# --------------------------------------------------------------------------

def _index_key(name: str, cfg: dict):
    for key in ("SP500", "NDX100"):
        if re.search(cfg["index_patterns"][key], name, flags=re.I):
            return key
    return None


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name or ""))


def _company(name: str) -> str:
    for c in COMPANIES:
        if c in name:
            return c
    head = re.split(r"标普|标准普尔|纳斯达克|纳指|S&P|SP500", name)[0]
    return (head or "其他")[:6]


def _meta(code: str, name: str, index_key: str, venue: str, cfg: dict) -> dict:
    name = _clean_name(name)
    currency = "USD" if any(w in name for w in USD_WORDS) else "CNY"
    letters = re.findall(r"(?<![A-Za-z])([ACDEFI])(?![A-Za-z])", name)
    share_class = letters[-1] if letters else ""
    return {"code": code, "name": name, "index_key": index_key, "venue": venue,
            "currency": currency, "share_class": share_class,
            "family": f"{index_key}|{_company(name)}"}


def discover(names_df, etf_df, lof_df, cfg: dict, venue_hint: dict | None = None) -> list:
    found = {}
    excl = re.compile(cfg["exclude_pattern"], flags=re.I)
    type_pat = re.compile(cfg["fund_type_pattern"], flags=re.I)
    for _, r in names_df.iterrows():
        name = _clean_name(r.get("基金简称"))
        key = _index_key(name, cfg)
        if not key or excl.search(name):
            continue
        if not type_pat.search(str(r.get("基金类型", ""))):
            continue
        code = str(r.get("基金代码", "")).zfill(6)
        found[code] = _meta(code, name, key, "OTC", cfg)
    etf_codes = set(etf_df["代码"].astype(str)) if not etf_df.empty else set()
    lof_codes = set(lof_df["代码"].astype(str)) if not lof_df.empty else set()
    for df, venue in ((etf_df, "ETF"), (lof_df, "LOF")):
        if df.empty:
            continue
        for _, r in df.iterrows():
            name = _clean_name(r.get("名称"))
            key = _index_key(name, cfg)
            if not key or excl.search(name):
                continue
            code = str(r.get("代码", "")).zfill(6)
            item = _meta(code, name, key, venue, cfg)
            if code in found:
                old = found[code]
                item["name"] = old["name"] if len(old["name"]) > len(name) else name
                item["family"] = old["family"]
                item["venue"] = venue
            found[code] = item
    for code, item in found.items():
        if code in etf_codes:
            item["venue"] = "ETF"
        elif code in lof_codes:
            item["venue"] = "LOF"
    # 行情兜底日：以上次成功快照的场所归属为准。兜底行情表只提供价格，
    # 不允许把一个场外基金「提升」成场内，否则就会出现假溢价。
    if venue_hint:
        for code, item in found.items():
            known = venue_hint.get(code)
            if known in ("ETF", "LOF", "OTC"):
                item["venue"] = known
    groups = defaultdict(list)
    for item in found.values():
        groups[item["family"]].append(item)
    for _, items in groups.items():
        cny = [x for x in items if x["currency"] == "CNY"]
        order = {"ETF": 0, "LOF": 1, "OTC": 2}
        cny.sort(key=lambda x: (order.get(x["venue"], 3),
                                0 if x["share_class"] in ("A", "") else 1, x["code"]))
        primary = (cny or items)[0]["code"]
        for x in items:
            x["primary_code"] = primary
    return list(found.values())


def sync_registry(items: list, registry_path: Path = REGISTRY_PATH) -> tuple:
    old = {}
    if registry_path.exists():
        try:
            old = json.loads(registry_path.read_text(encoding="utf-8")).get("funds", {})
        except Exception:  # noqa: BLE001
            old = {}
    today = sources.bj_today()
    new_codes, gone_codes = [], []
    cur = {x["code"] for x in items}
    for code, rec in old.items():
        if code not in cur and rec.get("status") != "delisted":
            rec["status"] = "delisted"
            rec["last_seen"] = today
            gone_codes.append(code)
    for item in items:
        code = item["code"]
        if code not in old:
            new_codes.append(code)
            old[code] = {"name": item["name"], "index_key": item["index_key"],
                         "first_seen": today, "status": "active",
                         "source": "auto", "override_reason": ""}
        else:
            rec = old[code]
            if rec.get("name") != item["name"]:
                rec["name"] = item["name"]
            if rec.get("status") == "delisted":
                rec["status"] = "active"
            rec.setdefault("first_seen", today)
        item["status"] = old[code].get("status", "active")
        item["first_seen"] = old[code].get("first_seen", today)
        if old[code].get("status") == "excluded":
            item["status"] = "excluded"
    registry_path.write_text(
        json.dumps({"updated_at": sources.bj_now().isoformat(timespec="seconds"),
                    "funds": old}, ensure_ascii=False, indent=2), encoding="utf-8")
    return [x for x in items if x.get("status") != "excluded"], new_codes, gone_codes


# --------------------------------------------------------------------------
# 状态文件（替代本地版 SQLite）
# --------------------------------------------------------------------------

def load_state() -> dict:
    empty = {"updated_at": "", "series": {}, "bench": {}, "fx": [],
             "premium_history": {}, "scale_cache": {}}
    if not STATE_PATH.exists():
        return empty
    try:
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        for k, v in empty.items():
            s.setdefault(k, v)
        return s
    except Exception:  # noqa: BLE001
        return empty


def load_seed() -> dict:
    try:
        return json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"meta": {}, "columns": COLUMNS, "rows": [], "summary": {}}


def series_df(state: dict, code: str, kind: str) -> pd.DataFrame:
    rec = state.get("series", {}).get(code, {})
    arr = rec.get(kind) or []
    if not arr:
        return pd.DataFrame(columns=["date", "nav" if kind == "nav" else "close"])
    df = pd.DataFrame(arr, columns=["date", "nav" if kind == "nav" else "close"])
    df["date"] = pd.to_datetime(df["date"].map(sources._to_dt)).astype(sources.DT)
    return df.dropna().sort_values("date").reset_index(drop=True)


def _trim(arr: list, keep: int) -> list:
    return list(arr[-keep:]) if len(arr) > keep else list(arr)


def save_series(state: dict, code: str, kind: str, df: pd.DataFrame):
    if df is None or df.empty:
        return
    col = "nav" if kind == "nav" else "close"
    rec = state["series"].setdefault(code, {})
    old = {str(d): float(v) for d, v in (rec.get(kind) or [])}
    for d, v in zip(df["date"], df[col]):
        old[str(pd.Timestamp(d).date())] = float(v)
    rec[kind] = _trim([[d, old[d]] for d in sorted(old)], CFG.get("history_keep", 320))
    rec[f"{kind}_at"] = sources.bj_now().isoformat(timespec="seconds")


def hist_fresh(state: dict, code: str, kind: str) -> bool:
    at = state.get("series", {}).get(code, {}).get(f"{kind}_at")
    if not at:
        return False
    try:
        delta = sources.bj_now() - dt.datetime.fromisoformat(at)
        return delta.days < CFG.get("nav_history_refresh_days", 7) and delta.total_seconds() > -3600
    except Exception:  # noqa: BLE001
        return False


def bench_from_state(state: dict, key: str, currency: str) -> pd.DataFrame:
    arr = (state.get("bench") or {}).get(f"{key}|{currency}") or []
    if not arr:
        return pd.DataFrame(columns=["date", "bench"])
    df = pd.DataFrame(arr, columns=["date", "bench"])
    df["date"] = pd.to_datetime(df["date"].map(sources._to_dt)).astype(sources.DT)
    return df.sort_values("date").reset_index(drop=True)


def save_bench(state: dict, key: str, currency: str, df: pd.DataFrame):
    if df is None or df.empty:
        return
    state["bench"][f"{key}|{currency}"] = _trim(
        [[str(pd.Timestamp(d).date()), round(float(v), 6)] for d, v in zip(df["date"], df["bench"])],
        CFG.get("history_keep", 320))


def build_benchmarks(raw: dict, state: dict) -> dict:
    fx = raw["fx"]
    if (fx is None or fx.empty) and state.get("fx"):
        fx = pd.DataFrame(state["fx"], columns=["date", "close"])
        fx["date"] = pd.to_datetime(fx["date"].map(sources._to_dt)).astype(sources.DT)
        sources.STATUS["warnings"].append("汇率改用上一次成功快照缓存")
    if fx is None:
        fx = pd.DataFrame(columns=["date", "close"])
    if not fx.empty:
        state["fx"] = _trim([[str(pd.Timestamp(d).date()), float(v)]
                             for d, v in zip(fx["date"], fx["close"])],
                            CFG.get("history_keep", 320))
    bench = {}
    for key in ("SP500", "NDX100"):
        idx = (raw.get("index") or {}).get(key)
        if idx is not None and not idx.empty:
            bench[key] = {
                "CNY": metrics.build_benchmark(idx, fx, "CNY"),
                "USD": metrics.build_benchmark(idx, fx, "USD"),
            }
        else:
            sources.STATUS["warnings"].append(f"{key} 指数改用上一次成功快照缓存")
            bench[key] = {"CNY": bench_from_state(state, key, "CNY"),
                          "USD": bench_from_state(state, key, "USD")}
        for c in ("CNY", "USD"):
            save_bench(state, key, c, bench[key][c])
    return bench


def fetch_histories(state: dict, rows: list, progress=print):
    need_nav = [r for r in rows if not hist_fresh(state, r["code"], "nav")]
    need_px = [r for r in rows if r["venue"] in ("ETF", "LOF")
               and not hist_fresh(state, r["code"], "price")]
    progress(f"补历史：净值 {len(need_nav)} 只 / 场内价格 {len(need_px)} 只 ...")

    def _nav(r):
        cached = series_df(state, r["code"], "nav")
        return r["code"], "nav", sources.nav_history(r["code"], r["venue"], cached)

    def _px(r):
        cached = series_df(state, r["code"], "price")
        return r["code"], "price", sources.price_history(r["code"], cached)

    jobs = [(r, _nav) for r in need_nav] + [(r, _px) for r in need_px]
    ok = 0
    if not jobs:
        return 0, 0
    pool = ThreadPoolExecutor(max_workers=CFG.get("history_workers", 6))
    try:
        futs = {pool.submit(fn, r): r["code"] for r, fn in jobs}
        try:
            for fut in as_completed(futs, timeout=max(180, len(jobs) * 3)):
                try:
                    code, kind, df = fut.result(timeout=CFG.get("history_timeout_sec", 40))
                    if df is not None and not df.empty:
                        save_series(state, code, kind, df)
                        ok += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("历史任务异常: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("历史补数整体超时: %s", exc)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    progress(f"历史补数完成 {ok}/{len(jobs)}")
    return ok, len(jobs)


def fill_scale_gaps(state: dict, codes: list, progress=print):
    """季报净资产直连补齐，结果缓存 30 天，避免每天打几十次 F10/pingzhongdata。"""
    cache = state.setdefault("scale_cache", {})
    now = sources.bj_now()
    todo = []
    for c in codes:
        rec = cache.get(c)
        if rec and rec.get("net_asset_yi"):
            try:
                if (now - dt.datetime.fromisoformat(rec["at"])).days < CFG.get("scale_cache_days", 30):
                    continue
            except Exception:  # noqa: BLE001
                pass
        todo.append(c)
    if not todo:
        return cache
    progress(f"补 {len(todo)} 只基金的季报净资产 ...")
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        futs = {pool.submit(sources.direct_pz_scale, c): c for c in todo}
        for fut in as_completed(futs, timeout=180):
            c = futs[fut]
            try:
                v = fut.result(timeout=25)
                if v:
                    v["at"] = now.isoformat(timespec="seconds")
                    cache[c] = v
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        log.warning("季报规模批量超时: %s", exc)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    progress(f"季报规模命中 {len(cache)}/{len(codes)}")
    return cache


# --------------------------------------------------------------------------
# 组装行
# --------------------------------------------------------------------------

def _f(v):
    try:
        x = float(v)
        return x if np.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def build_rows(rows: list, data: dict, cfg: dict, state: dict) -> list:
    etf = data["etf_spot"].set_index("代码") if not data["etf_spot"].empty else pd.DataFrame()
    lof = data["lof_spot"].set_index("代码") if not data["lof_spot"].empty else pd.DataFrame()
    nav_map, purchase, scale, jsl = data["nav_map"], data["purchase"], data["scale"], data["jsl"]
    bench, price_date = data["bench"], data["price_date"]
    worker_p = data.get("worker_premium") or {}
    seed_rows = {r["code"]: r for r in data["seed"].get("rows", [])}

    def _seed(code, field):
        return (seed_rows.get(code) or {}).get(field)

    for r in rows:
        code = r["code"]
        spot = None
        if r["venue"] == "ETF" and code in etf.index:
            spot = etf.loc[code]
        elif r["venue"] == "LOF" and len(lof) and code in lof.index:
            spot = lof.loc[code]
        if isinstance(spot, pd.DataFrame):
            spot = spot.iloc[0]
        wp = worker_p.get(code) or {}

        price = _f(spot.get("最新价")) if spot is not None else None
        iopv = _f(spot.get("IOPV实时估值")) if spot is not None else None
        amount = _f(spot.get("成交额")) if spot is not None else None
        mktcap = _f(spot.get("流通市值")) if spot is not None else None
        shares = _f(spot.get("最新份额")) if spot is not None else None
        bid = ask = None
        if spot is not None:
            bid, ask = _f(spot.get("买一")), _f(spot.get("卖一"))
        # Worker 第二意见：只在主源缺失时补位，并统计交叉校验覆盖
        if price is None:
            price = _f(wp.get("price"))
        if price is not None and bid is None:
            bid = ask = None
        # 上次快照的价格/成交额只对场内份额有意义。场外按净值申赎、不该有场内价；
        # 若上一版快照被污染（例如场外代码被当 A 股股票取过价），这里会把错价继承下来。
        if price is None and r["venue"] in ("ETF", "LOF"):
            price = _f(_seed(code, "price"))
        if amount is None and r["venue"] in ("ETF", "LOF"):
            amount = (_f(_seed(code, "amount_yi")) or 0) * 1e8 or None

        navinfo = nav_map.get(code, {})
        nav = _f(navinfo.get("nav"))
        nav_date = navinfo.get("nav_date")
        if nav is None and _f(wp.get("nav")):
            nav, nav_date = _f(wp["nav"]), wp.get("nav_date") or nav_date
        if nav is None and code in jsl:
            nav = _f(jsl[code].get("t2_nav"))
            nav_date = jsl[code].get("nav_date") or nav_date
        if nav is None:
            hist = series_df(state, code, "nav")
            if not hist.empty:
                nav = _f(hist.iloc[-1]["nav"])
                nav_date = str(hist.iloc[-1]["date"])[:10]
        if nav is None:
            nav, nav_date = _f(_seed(code, "nav")), _seed(code, "nav_date")
        if not nav_date:
            nav_date = None

        est_nav = ratio = us0 = us1 = None
        est_status = "fail"
        if price is not None and nav is not None and nav_date and r["currency"] == "CNY":
            b = bench.get(r["index_key"], {}).get("CNY")
            est_nav, ratio, us0, us1, est_status = metrics.estimate_nav(
                nav, nav_date, price_date, b, cfg.get("fallback_max_nav_age_days", 7))
            if est_nav is not None:
                est_nav, ratio = round(est_nav, 6), round(ratio, 6)
        prem = metrics.compute_premium(price, nav, iopv, est_nav, cfg)
        r.update({
            "price": price, "nav": nav, "nav_date": nav_date,
            "est_nav": est_nav, "est_ratio": ratio,
            "us_nav_session": us0, "us_price_session": us1, "iopv": iopv,
            "static": prem["static"], "estimated": prem["estimated"],
            "iopv_premium": prem["iopv"], "primary": prem["primary"],
            "premium_kind": prem["premium_kind"], "downgraded": prem["downgraded"],
            "amount_yi": round(amount / 1e8, 3) if amount else _seed(code, "amount_yi"),
            "rejected": prem.get("rejected") or "",
        })
        if prem.get("rejected"):
            # 行情源代码错配：整行不保留任何价格与溢价数值，只留可解释的提示，
            # 避免假数字进入排行榜、KPI、散点图和 CSV。
            price = None
            r.update({"price": None, "est_nav": None, "est_ratio": None,
                      "iopv": None, "amount_yi": None,
                      "us_nav_session": None, "us_price_session": None})

        # 规模（统一折算成人民币亿元）
        scale_yi, scale_src, scale_date = None, "—", None
        if r["venue"] in ("ETF", "LOF") and mktcap and mktcap > 0:
            scale_yi, scale_src = mktcap / 1e8, "场内份额×净值（实时）"
        elif r["venue"] in ("ETF", "LOF") and shares and nav:
            scale_yi, scale_src = shares * nav / 1e8, "场内份额×净值（实时）"
        elif code in scale:
            s = scale[code]
            unit = s.get("scale_nav") or nav
            if unit:
                scale_yi = s["shares"] * unit / 1e8
                if r["currency"] == "USD":
                    scale_yi *= data.get("fx_latest") or 1.0
                scale_src, scale_date = "场外份额×净值（报告期）", s.get("scale_date")
        elif code in data.get("pz_scale", {}):
            s = data["pz_scale"][code]
            scale_yi, scale_src = s.get("net_asset_yi"), "季报净资产（含全部份额）"
            scale_date = s.get("scale_date")
        if scale_yi is None:
            scale_yi, scale_src = _seed(code, "scale_yi"), _seed(code, "scale_source") or "上次快照"
        r.update({"scale_yi": round(scale_yi, 3) if scale_yi else None,
                  "scale_source": scale_src, "scale_date": scale_date})

        pur = purchase.get(code, {})
        r.update({
            "purchase_status": pur.get("purchase_status") or _seed(code, "purchase_status") or "",
            "limit_amount": pur.get("limit_amount") if pur.get("limit_amount") is not None else _seed(code, "limit_amount"),
            "fee": _f(pur.get("fee")),
            "related": jsl.get(code, {}).get("related", "") or _seed(code, "related") or "",
            "spread": round((ask - bid) / price * 100, 3) if bid and ask and price else None,
        })

    metrics.mark_outliers(rows, cfg)

    # 跟踪度
    for r in rows:
        code = r["code"]
        nh = series_df(state, code, "nav")
        ph = series_df(state, code, "price") if r["venue"] in ("ETF", "LOF") else pd.DataFrame()
        b = bench.get(r["index_key"], {}).get(r["currency"])
        if b is None or b.empty:
            b = bench.get(r["index_key"], {}).get("CNY")
        tr = metrics.compute_tracking(nh, b, cfg.get("te_window", 250),
                                      cfg.get("min_te_samples", 60),
                                      ph if not ph.empty else None)
        # 基准整体失效时，沿用上次成功快照的跟踪度，避免整列变空
        if tr["te"] is None:
            tr = {**tr, "te": _seed(code, "te"), "td": _seed(code, "td"),
                  "r2": _seed(code, "r2"), "status": "沿用上次快照"}
        r.update({"te": tr["te"], "td": tr["td"], "r2": tr["r2"],
                  "te_n": tr["n"], "te_price": tr["te_price"],
                  "premium_noise": tr["premium_noise"], "te_status": tr["status"]})
    metrics.score_rows(rows, cfg["weights"])
    for r in rows:
        for c in COLUMNS:
            r.setdefault(c, None)
    return [{c: r.get(c) for c in COLUMNS} for r in rows]


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------

def write_llms(meta, rows, summary, status):
    ok = [k for k, v in status.get("chains", {}).items() if not v.get("failed")]
    bad = status.get("degraded_chains") or []
    lines = [
        "# 国内标普500 / 纳斯达克100 QDII 溢价与跟踪度榜单（线上版）",
        "",
        f"数据时间：{meta.get('price_date')}；生成时间：{meta.get('generated_at')}（北京时间）",
        f"主口径：{meta.get('primary_kind')}；池内 {summary.get('total')} 只代码，"
        f"其中场内 {summary.get('on_exchange')} 只、场外 {summary.get('off_exchange')} 只。",
        f"数据源：命中 {len(ok)} 条链路" + (f"，降级 {len(bad)} 条（{'、'.join(bad)}）" if bad else "，全部正常") + "。",
        "",
        "## 口径说明",
        "- 静态溢价 = 场内价 / 最近披露单位净值 − 1（净值滞后 1~2 日，仅作留痕）。",
        "- 估算溢价 = 场内价 / 估算净值 − 1；估算净值 = 披露净值 × 基准(价格对应美股日) / 基准(净值对应美股日)，基准 = 美元指数 × USD/CNY。",
        "- 净值日取不晚于它的最近美股收盘，价格日取严格早于它的最近美股收盘；两者同一天时放大系数为 1（估算＝静态），只有净值真正滞后才补偿滞后期涨跌。",
        "- 估算不可用时自动降级为静态溢价并标注，绝不拿滞后净值冒充实时估值。",
        "- 跟踪度：年化跟踪误差 TE、年化跟踪偏离 TD、R²；净值序列按同日或之前最近美股收盘对齐。",
        "- 场外基金按净值申赎，本身无溢价；申购状态/限购金额是溢价成因的重要线索。",
        "- 本页每天北京时间 21:00 自动刷新一次，四层数据源互备；所有源同时失效时展示上一次成功快照并标注陈旧。",
        "",
        "## 估算溢价最高（场内）",
    ]
    tops = sorted([r for r in rows if r["primary"] is not None],
                  key=lambda x: x["primary"], reverse=True)[:10]
    for r in tops:
        lines.append(f"- {r['code']} {r['name']}：{r['primary']:+.2f}%"
                     f"（静态 {r['static']:+.2f}%，净值日 {r['nav_date']}，"
                     f"规模 {r['scale_yi'] or 0:.1f}亿，TE {r['te'] if r['te'] is not None else '—'}%，"
                     f"申购 {r['purchase_status'] or '—'}）")
    lines += ["", "## 综合评分前十", ""]
    for r in sorted([x for x in rows if x["score"] is not None],
                    key=lambda x: x["score"], reverse=True)[:10]:
        lines.append(f"- {r['code']} {r['name']}：{r['score']:.1f} 分"
                     f"（溢价 {r['primary'] if r['primary'] is not None else '—'}%，"
                     f"TE {r['te'] if r['te'] is not None else '—'}%）")
    lines += ["", f"本次新增：{summary.get('new_count')} 只；"
                  f"估算降级：{summary.get('downgraded')} 只；口径存疑：{summary.get('doubt')} 只。",
              "", CFG.get("disclaimer", ""), ""]
    (DIST / "llms.txt").write_text("\n".join(lines), encoding="utf-8")


def render_html(data: dict) -> str:
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return tpl.replace("{{ data_json|safe }}", payload)


def export(rows, meta, summary, status, premium_history, state, stale=False):
    DIST.mkdir(parents=True, exist_ok=True)
    data = {"meta": meta, "columns": COLUMNS, "rows": rows, "summary": summary,
            "premium_history": premium_history, "source_status": status}
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    (DIST / "latest.json").write_text(payload, encoding="utf-8")
    if not stale:
        # 只有成功刷新才更新兜底快照，避免把降级结果固化成「上次好数据」
        (DIST / "seed.json").write_text(
            json.dumps({"meta": meta, "columns": COLUMNS, "rows": rows, "summary": summary},
                       ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    else:
        (DIST / "seed.json").write_text(SEED_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    cols = ["code", "name", "index_key", "venue", "currency", "share_class", "primary_code",
            "price", "nav", "nav_date", "est_nav", "static", "estimated", "iopv_premium",
            "primary", "premium_kind", "downgraded", "quality", "scale_yi", "amount_yi",
            "te", "td", "r2", "premium_noise", "purchase_status", "limit_amount", "score",
            "doubt", "rejected"]
    pd.DataFrame([{c: r.get(c) for c in cols} for r in rows])[cols].to_csv(
        DIST / "ranking.csv", index=False, encoding="utf-8-sig")
    write_llms(meta, rows, summary, status)
    (DIST / "source_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    (DIST / "history.json").write_text(
        json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (DIST / "registry.json").write_text(REGISTRY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    (DIST / "index.html").write_text(render_html(data), encoding="utf-8")
    (DIST / ".nojekyll").write_text("", encoding="utf-8")
    (DIST / "meta.json").write_text(json.dumps({
        "generated_at": meta.get("generated_at"), "bj_date": meta.get("bj_date"),
        "price_date": meta.get("price_date"), "stale": stale,
        "total": summary.get("total"),
    }, ensure_ascii=False), encoding="utf-8")
    return data


def update_premium_history(state: dict, rows: list, price_date: str) -> dict:
    hist = state.setdefault("premium_history", {})
    for r in rows:
        if r["primary"] is None:
            continue
        arr = hist.setdefault(r["code"], [])
        if not arr or arr[-1][0] != price_date:
            arr.append([price_date, r["primary"]])
        else:
            arr[-1][1] = r["primary"]
    cutoff = (sources.bj_now().date() - dt.timedelta(days=CFG.get("premium_history_keep_days", 400))).isoformat()
    for c, arr in hist.items():
        hist[c] = [[d, v] for d, v in arr if d >= cutoff][-CFG.get("premium_history_keep_days", 400):]
    return hist


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def publish_stale(seed: dict, reason: str, state: dict):
    """所有实时源都不可用时：原样发布上一次成功快照，明确打陈旧标。"""
    rows = seed.get("rows") or []
    meta = dict(seed.get("meta") or {})
    summary = dict(seed.get("summary") or {})
    meta.update({
        "stale": True, "stale_reason": reason,
        "generated_at": sources.bj_now().isoformat(timespec="seconds"),
        "bj_date": sources.bj_today(),
        "primary_kind": meta.get("primary_kind", "估算溢价"),
    })
    status = dict(sources.STATUS)
    status["stale"] = True
    status["stale_reason"] = reason
    export(rows, meta, summary, status, {}, state, stale=True)
    return {"ok": True, "stale": True, "rows": len(rows), "reason": reason}


def run(progress=print):
    t0 = time.time()
    DIST.mkdir(parents=True, exist_ok=True)
    state = load_state()
    seed = load_seed()
    seed_rows = seed.get("rows", [])
    # 腾讯兜底只请求「上次已确认的场内代码」：场外 6 位代码可能与 A 股
    # 股票代码重合（000055 = 广发纳指100联接美元 = 深市方大集团），
    # 拿去请求股票行情会算出 200%+ 的假溢价。
    seed_on_codes = [r["code"] for r in seed_rows
                     if r.get("venue") in ("ETF", "LOF")]
    venue_hint = {r["code"]: r.get("venue") for r in seed_rows}

    progress("1/6 四层数据源采集 ...")
    raw = sources.collect(seed, fallback_codes=seed_on_codes)

    progress("2/6 自动发现名单并比对 registry ...")
    items = discover(raw["names"], raw["etf_spot"], raw["lof_spot"], CFG,
                     venue_hint=venue_hint if raw.get("price_fallback_used") else None)
    active, new_codes, gone_codes = sync_registry(items)
    if not active:
        progress("名单发现为空，发布上一次成功快照（数据陈旧）")
        return publish_stale(seed, "本次全量基金名录与场内行情均未取到", state)

    progress("3/6 Worker 溢价交叉校验 / 季报规模 / 基准 ...")
    on_codes = [r["code"] for r in active if r["venue"] in ("ETF", "LOF")]
    raw["worker_premium"] = sources.worker_crosscheck(on_codes[:120])
    off_codes = [r["code"] for r in active
                 if r["code"] not in raw["scale"] and r["venue"] == "OTC"]
    raw["pz_scale"] = fill_scale_gaps(state, off_codes, progress)
    raw["bench"] = build_benchmarks(raw, state)

    progress("4/6 补历史净值与场内价格 ...")
    hist_ok, hist_total = fetch_histories(state, active, progress)

    progress("5/6 计算三层溢价、跟踪度、规模与评分 ...")
    rows = build_rows(active, raw, CFG, state)
    price_date = raw["price_date"]
    premium_history = update_premium_history(state, rows, price_date)
    state["updated_at"] = sources.bj_now().isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")

    priced = sum(1 for r in rows if r["price"] is not None)
    if priced == 0:
        progress("场内价格全部缺失，发布上一次成功快照（数据陈旧）")
        return publish_stale(seed, "本次场内行情源全部不可用", state)

    summary = {
        "total": len(rows),
        "on_exchange": sum(1 for r in rows if r["venue"] in ("ETF", "LOF")),
        "off_exchange": sum(1 for r in rows if r["venue"] == "OTC"),
        "sp500": sum(1 for r in rows if r["index_key"] == "SP500"),
        "ndx100": sum(1 for r in rows if r["index_key"] == "NDX100"),
        "new_count": len(new_codes), "new_codes": new_codes,
        "gone_count": len(gone_codes),
        "downgraded": sum(1 for r in rows if r["downgraded"]),
        "doubt": sum(1 for r in rows if r["quality"] == "存疑"),
        "te_ok": sum(1 for r in rows if r["te"] is not None),
        "hist_ok": hist_ok, "hist_total": hist_total,
        "priced": priced,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    meta = {
        "run_id": sources.bj_now().strftime("%Y%m%d%H%M%S"),
        "price_date": price_date,
        "bj_date": sources.bj_today(),
        "generated_at": sources.bj_now().isoformat(timespec="seconds"),
        "primary_kind": "估算溢价" if CFG.get("premium_primary") == "estimated" else "静态溢价",
        "fx_available": not raw["fx"].empty,
        "index_rows": {k: (0 if v is None else len(v)) for k, v in (raw["index"] or {}).items()},
        "fx_rows": len(raw["fx"]),
        "nav_date_sample": sorted({r["nav_date"] for r in rows if r["nav_date"]})[-3:],
        "weights": CFG["weights"], "te_window": CFG["te_window"],
        "scope": CFG["scope"], "disclaimer": CFG["disclaimer"],
        "stale": False,
        "schedule": "每天北京时间 21:00（UTC 13:00）自动刷新；21:30 / 23:00 仅在未成功时补跑",
    }
    # Worker 交叉校验是 collect() 之后才跑的，这里重新汇总核心/辅助降级
    chains = sources.STATUS.get("chains", {})
    sources.STATUS["degraded_chains"] = [k for k, v in chains.items()
                                         if v.get("failed") and not v.get("optional")]
    sources.STATUS["optional_degraded"] = [k for k, v in chains.items()
                                           if v.get("failed") and v.get("optional")]
    status = dict(sources.STATUS)
    status.update({
        "stale": False,
        "quiet_hits": dict(sources.QUIET_HITS),
        "worker_crosscheck": status.get("crosscheck", {}),
        "layers": {
            "L0": "a-stock-api Worker（fund-nav / etf-premium / trade-calendar）",
            "L1": "东方财富 / 腾讯 / 新浪 直连",
            "L2": "AKShare 批量表",
            "L3": "仓库内上次成功快照与历史序列",
        },
    })
    progress("6/6 导出静态页面与产物 ...")
    export(rows, meta, summary, status, premium_history, state)
    progress(f"完成：{summary['total']} 只（场内有效价 {priced}），新增 {len(new_codes)} 只，"
             f"降级链路 {len(status.get('degraded_chains') or [])} 条，用时 {summary['elapsed_sec']} 秒")
    return {"ok": True, "stale": False, "summary": summary, "meta": meta}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()
