"""指标计算：三层溢价、跟踪度、规模、评分。

核心口径（详见 README）：
  净值日 → 不晚于它的最近美股收盘（同日匹配；与基准互相关实测 corr≈0.95）。
  价格日 → 严格早于它的最近美股收盘（A 股收盘时美股当日尚未开盘）。
  估算净值 = 最新披露净值 × 基准(价格对应美股日) / 基准(净值对应美股日)；
  两者是同一美股日时放大系数为 1，估算溢价＝静态溢价；
  只有净值披露真正滞后时才补偿滞后期内的指数与汇率涨跌。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------
# 基准序列
# --------------------------------------------------------------------------

def build_benchmark(index_df: pd.DataFrame, fx_df: pd.DataFrame, currency: str = "CNY") -> pd.DataFrame:
    """人民币份额用「美元指数 × USD/CNY」，美元份额只用美元指数。"""
    dt = "datetime64[ns]"
    idx = index_df.copy()
    idx["date"] = pd.to_datetime(idx["date"]).astype(dt)
    idx = idx.sort_values("date")
    if currency == "USD" or fx_df is None or fx_df.empty:
        idx["bench"] = idx["close"].astype(float)
    else:
        fx = fx_df.copy()
        fx["date"] = pd.to_datetime(fx["date"]).astype(dt)
        fx = fx.rename(columns={"close": "fx"}).sort_values("date")
        merged = pd.merge_asof(idx, fx[["date", "fx"]], on="date", direction="backward")
        merged["fx"] = merged["fx"].ffill().bfill()
        idx["bench"] = merged["close"].astype(float) * merged["fx"].astype(float)
    return idx[["date", "bench"]].dropna().reset_index(drop=True)


def us_session_for(aday, bench: pd.DataFrame, strict: bool = True):
    """aday 对应的美股收盘日及其基准值。

    strict=True  ：严格早于 aday 的最近美股交易日 —— 用于 A 股「价格日」
                   （A 股 T 日收盘时，美股 T 日尚未开盘，最近收盘是 T-1）。
    strict=False ：不晚于 aday 的最近美股交易日 —— 用于「净值日」
                   （实测 QDII 净值日期 T 对应同日历日的境外收盘，公布在 T+1）。
    该差异由净值/价格序列与基准的互相关实测校准：净值同日匹配时
    corr≈0.95~0.97、TE≈4~5%，严格早于匹配时 corr≈0、TE≈30%。
    """
    if aday is None or bench is None or bench.empty:
        return None
    ts = pd.Timestamp(aday)
    sub = bench[bench["date"] < ts] if strict else bench[bench["date"] <= ts]
    if sub.empty:
        return None
    return sub.iloc[-1]


# --------------------------------------------------------------------------
# 三层溢价
# --------------------------------------------------------------------------

def estimate_nav(nav: float, nav_date, price_date, bench: pd.DataFrame, max_age_days: int = 7):
    """返回 (估算净值, 放大系数, 净值对应美股日, 估值对应美股日, 状态)。

    状态 est / flat（同一美股日，无需调整） / fail（数据不足，应降级静态）。
    """
    if not nav or nav <= 0 or bench is None or bench.empty:
        return None, None, None, None, "fail"
    r0 = us_session_for(nav_date, bench, strict=False)
    r1 = us_session_for(price_date, bench, strict=True)
    if r0 is None or r1 is None:
        return None, None, None, None, "fail"
    try:
        age = (pd.Timestamp(price_date) - pd.Timestamp(nav_date)).days
    except Exception:  # noqa: BLE001
        age = 999
    if age > max_age_days or age < 0:
        return None, None, None, None, "fail"
    if r1["date"] <= r0["date"]:
        ratio = 1.0
        status = "flat"
    else:
        ratio = float(r1["bench"]) / float(r0["bench"])
        status = "est"
    if not np.isfinite(ratio) or ratio <= 0 or ratio > 1.6 or ratio < 0.6:
        return None, None, None, None, "fail"
    return nav * ratio, ratio, r0["date"].date().isoformat(), r1["date"].date().isoformat(), status


def compute_premium(price, nav, iopv, est_nav, cfg):
    """返回三层溢价（百分数）与主口径、降级标记。

    优先估算溢价；估算不可用时按配置降级到静态溢价并标注。
    """
    def _p(v):
        if v is None or not np.isfinite(v) or v <= 0:
            return None
        return round((price / v - 1) * 100, 3)

    if price is None or not np.isfinite(price) or price <= 0:
        return {"static": None, "estimated": None, "iopv": None, "primary": None,
                "premium_kind": "na", "downgraded": False}

    p_static = _p(nav)
    p_est = _p(est_nav)
    p_iopv = _p(iopv)
    primary = cfg.get("premium_primary", "estimated")
    downgraded = False
    if primary == "estimated":
        if p_est is None:
            if cfg.get("fallback_to_static", True) and p_static is not None:
                chosen, kind, downgraded = p_static, "static", True
            else:
                chosen, kind = p_est, "estimated"
        else:
            chosen, kind = p_est, "estimated"
    else:
        chosen, kind = p_static, "static"

    return {
        "static": p_static,
        "estimated": p_est,
        "iopv": p_iopv,
        "primary": chosen,
        "premium_kind": kind,
        "downgraded": downgraded,
    }


def mark_outliers(rows: list, cfg: dict):
    """同指数内横向自查：估算与静态差过大、或明显偏离同组中位数则打存疑标。"""
    gap_static = cfg.get("outlier_static_gap_pp", 6.0)
    gap_peer = cfg.get("outlier_peer_gap_pp", 10.0)
    groups = {}
    for r in rows:
        if r.get("primary") is not None:
            groups.setdefault(r.get("index_key"), []).append(r["primary"])
    meds = {k: float(np.median(v)) for k, v in groups.items() if v}
    for r in rows:
        flags = []
        p = r.get("primary")
        s = r.get("static")
        if p is not None and s is not None and abs(p - s) > gap_static:
            flags.append(f"估算与静态差 {abs(p - s):.1f}pp")
        med = meds.get(r.get("index_key"))
        if p is not None and med is not None and abs(p - med) > gap_peer:
            flags.append(f"偏离同指数中位数 {abs(p - med):.1f}pp")
        if r.get("downgraded"):
            flags.append("估算不可用已降级静态")
        r["doubt"] = "；".join(flags)
        r["quality"] = "存疑" if flags else "正常"
    return rows


# --------------------------------------------------------------------------
# 跟踪度
# --------------------------------------------------------------------------

def _clean_returns(nav_series: pd.Series, bench_series: pd.Series):
    rf = nav_series.pct_change()
    rb = bench_series.pct_change()
    mask = (rf.abs() < 0.25) & (rb.abs() < 0.25) & rf.notna() & rb.notna()
    return rf[mask], rb[mask]


def compute_tracking(nav_hist: pd.DataFrame, bench: pd.DataFrame, window: int = 250,
                     min_samples: int = 60, price_hist: pd.DataFrame | None = None):
    """年化跟踪误差 TE、年化跟踪偏离 TD、R²，以及溢价扰动。

    用 merge_asof 把每个净值日对齐到「严格早于它的最近美股交易日」的基准值。
    """
    empty = {"te": None, "td": None, "r2": None, "n": 0, "te_price": None,
             "premium_noise": None, "status": "样本不足"}
    if nav_hist is None or nav_hist.empty or bench is None or bench.empty:
        return empty
    dt = "datetime64[ns]"
    left = nav_hist.copy()
    left["date"] = pd.to_datetime(left["date"]).astype(dt)
    left = left.sort_values("date").tail(max(window + 5, min_samples + 5)).copy()
    right = bench.copy()
    right["date"] = pd.to_datetime(right["date"]).astype(dt)
    right = right.sort_values("date")
    try:
        merged = pd.merge_asof(
            left[["date", "nav"]], right,
            on="date", direction="backward", allow_exact_matches=True,
        )
    except Exception:  # noqa: BLE001
        return empty
    merged = merged.dropna(subset=["nav", "bench"]).tail(window)
    if len(merged) < min_samples:
        return {**empty, "n": len(merged)}
    rf, rb = _clean_returns(merged["nav"], merged["bench"])
    if len(rf) < min_samples:
        return {**empty, "n": len(rf)}
    diff = rf - rb
    te = float(diff.std(ddof=1) * math.sqrt(252) * 100)
    ann_f = float((1 + rf).prod() ** (252 / len(rf)) - 1) * 100
    ann_b = float((1 + rb).prod() ** (252 / len(rb)) - 1) * 100
    td = ann_f - ann_b
    corr = float(rf.corr(rb))
    r2 = corr ** 2 if np.isfinite(corr) else None

    te_price, noise = None, None
    if price_hist is not None and not price_hist.empty:
        ph = price_hist.copy()
        ph["date"] = pd.to_datetime(ph["date"]).astype(dt)
        ph = ph.sort_values("date")
        pm = pd.merge_asof(
            ph[["date", "close"]], right,
            on="date", direction="backward", allow_exact_matches=False,
        ).dropna(subset=["close", "bench"]).tail(window)
        if len(pm) >= min_samples:
            rp, rbp = _clean_returns(pm["close"], pm["bench"])
            if len(rp) >= min_samples:
                te_price = float((rp - rbp).std(ddof=1) * math.sqrt(252) * 100)
                noise = te_price - te

    return {
        "te": round(te, 3),
        "td": round(td, 3),
        "r2": round(r2, 4) if r2 is not None else None,
        "n": int(len(rf)),
        "te_price": round(te_price, 3) if te_price is not None else None,
        "premium_noise": round(noise, 3) if noise is not None else None,
        "status": "正常",
    }


# --------------------------------------------------------------------------
# 综合评分
# --------------------------------------------------------------------------

def score_rows(rows: list, weights: dict):
    """各维度转百分位后加权。溢价/跟踪误差越低越好，规模/成交额越高越好。"""
    if not rows:
        return rows
    df = pd.DataFrame(rows)

    def _low(s):
        s = pd.to_numeric(s, errors="coerce")
        return 1.0 - s.rank(pct=True)

    def _high(s):
        s = pd.to_numeric(s, errors="coerce")
        return np.log1p(s.clip(lower=0)).rank(pct=True)

    parts, used = [], []
    for key, col, fn in [
        ("premium", "primary", _low),
        ("tracking", "te", _low),
        ("scale", "scale_yi", _high),
        ("liquidity", "amount_yi", _high),
    ]:
        w = float(weights.get(key, 0))
        if w <= 0 or col not in df.columns:
            continue
        s = fn(df[col]).fillna(0.5)
        parts.append(w * s)
        used.append(key)
    total_w = sum(float(weights.get(k, 0)) for k in used) or 1.0
    scores = sum(parts) / total_w
    df["score"] = (scores * 100).round(1)

    def _rank_pct(s):
        return pd.to_numeric(s, errors="coerce").rank(pct=True)

    df["p_premium"] = (100 * (1 - _rank_pct(df["primary"]))).round(1)
    df["p_tracking"] = (100 * (1 - _rank_pct(df["te"]))).round(1)
    df["p_scale"] = (100 * _high(df["scale_yi"])).round(1)
    df["p_liquidity"] = (100 * _high(df["amount_yi"])).round(1)
    for i, r in enumerate(rows):
        r["score"] = None if pd.isna(df.loc[i, "score"]) else float(df.loc[i, "score"])
        for k in ("p_premium", "p_tracking", "p_scale", "p_liquidity"):
            v = df.loc[i, k]
            r[k] = None if pd.isna(v) else float(v)
    return rows
