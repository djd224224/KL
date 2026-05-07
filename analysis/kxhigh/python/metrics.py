"""Model-validation and execution metrics for KXHIGH."""
from __future__ import annotations

import numpy as np
import pandas as pd


def brier_score(p: np.ndarray, y: np.ndarray) -> float:
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    mask = np.isfinite(p) & np.isfinite(y)
    if mask.sum() == 0:
        return float("nan")
    return float(np.mean((p[mask] - y[mask]) ** 2))


def log_loss(p: np.ndarray, y: np.ndarray, eps: float = 1e-12) -> float:
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    mask = np.isfinite(p) & np.isfinite(y)
    p, y = np.clip(p[mask], eps, 1 - eps), y[mask]
    if len(p) == 0:
        return float("nan")
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier_decomposition(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> dict:
    """Returns reliability, resolution, uncertainty; Brier = REL - RES + UNC."""
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    mask = np.isfinite(p) & np.isfinite(y)
    p, y = p[mask], y[mask]
    if len(p) == 0:
        return {"reliability": float("nan"), "resolution": float("nan"),
                "uncertainty": float("nan"), "brier": float("nan"), "n": 0}
    bins = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(p, bins) - 1, 0, n_bins - 1)
    base_rate = y.mean()
    rel, res = 0.0, 0.0
    for b in range(n_bins):
        m = idx == b
        if m.sum() == 0:
            continue
        w = m.mean()
        p_bar, y_bar = p[m].mean(), y[m].mean()
        rel += w * (p_bar - y_bar) ** 2
        res += w * (y_bar - base_rate) ** 2
    unc = base_rate * (1 - base_rate)
    return {"reliability": rel, "resolution": res, "uncertainty": unc,
            "brier": rel - res + unc, "n": int(len(p))}


def reliability_bins(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> pd.DataFrame:
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    mask = np.isfinite(p) & np.isfinite(y)
    p, y = p[mask], y[mask]
    bins = np.linspace(0, 1, n_bins + 1)
    idx = np.clip(np.digitize(p, bins) - 1, 0, n_bins - 1)
    rows = []
    for b in range(n_bins):
        m = idx == b
        rows.append({
            "bin_low": bins[b],
            "bin_high": bins[b + 1],
            "bin_mid": (bins[b] + bins[b + 1]) / 2,
            "n": int(m.sum()),
            "mean_predicted": float(p[m].mean()) if m.sum() else np.nan,
            "actual_yes_rate": float(y[m].mean()) if m.sum() else np.nan,
        })
    return pd.DataFrame(rows)


def forecast_accuracy(df: pd.DataFrame, source_cols=("nws", "accuweather", "weather_underground")) -> pd.DataFrame:
    """MAE/RMSE/bias per source × city, using actual_high_estimate as ground truth."""
    out = []
    for src in source_cols:
        if src not in df.columns:
            continue
        s = df[[src, "actual_high_estimate", "city"]].dropna()
        if len(s) == 0:
            continue
        for city, g in s.groupby("city"):
            err = g[src] - g["actual_high_estimate"]
            out.append({
                "source": src, "city": city, "n": len(g),
                "mae": err.abs().mean(),
                "rmse": float(np.sqrt((err ** 2).mean())),
                "bias": err.mean(),
            })
        err_all = s[src] - s["actual_high_estimate"]
        out.append({"source": src, "city": "__all__", "n": len(s),
                    "mae": err_all.abs().mean(),
                    "rmse": float(np.sqrt((err_all ** 2).mean())),
                    "bias": err_all.mean()})
    return pd.DataFrame(out)


def spread_vs_pnl(df: pd.DataFrame, spread_col: str = "forecast_std", n_bins: int = 5) -> pd.DataFrame:
    s = df[[spread_col, "pnl"]].dropna()
    if len(s) == 0:
        return pd.DataFrame()
    s = s.copy()
    s["bin"] = pd.qcut(s[spread_col], q=n_bins, duplicates="drop")
    return (
        s.groupby("bin", observed=True)
         .agg(n=("pnl", "size"), mean_pnl=("pnl", "mean"),
              total_pnl=("pnl", "sum"), win_rate=("pnl", lambda x: (x > 0).mean()),
              median_spread=(spread_col, "median"))
         .reset_index()
    )


def pnl_attribution(df: pd.DataFrame, by: str) -> pd.DataFrame:
    if by not in df.columns:
        return pd.DataFrame()
    g = df.groupby(by).agg(
        n=("pnl", "size"),
        total_pnl=("pnl", "sum"),
        mean_pnl=("pnl", "mean"),
        win_rate=("pnl", lambda x: (x > 0).mean()),
        total_cost=("total_cost", "sum") if "total_cost" in df.columns else ("pnl", "size"),
    ).reset_index()
    g["roi"] = g["total_pnl"] / g["total_cost"].where(g["total_cost"] > 0, np.nan)
    return g.sort_values("total_pnl", ascending=False)


def fill_rate_by(df_orders: pd.DataFrame, by: str) -> pd.DataFrame:
    g = df_orders.groupby(by).agg(
        n_orders=("client_order_id", "size"),
        n_filled=("any_fill", "sum"),
        total_ordered=("ordered_contracts", "sum"),
        total_filled=("filled_contracts", "sum"),
    ).reset_index()
    g["fill_rate_orders"] = g["n_filled"] / g["n_orders"]
    g["fill_rate_contracts"] = g["total_filled"] / g["total_ordered"].where(g["total_ordered"] > 0, np.nan)
    return g.sort_values("n_orders", ascending=False)


def cumulative_pnl_series(df_settle: pd.DataFrame) -> pd.DataFrame:
    s = df_settle.dropna(subset=["settled_time", "pnl"]).copy()
    s["settled_ts"] = pd.to_datetime(s["settled_time"], errors="coerce", utc=True)
    s = s.dropna(subset=["settled_ts"]).sort_values("settled_ts")
    s["cum_pnl"] = s["pnl"].cumsum()
    s["peak"] = s["cum_pnl"].cummax()
    s["drawdown"] = s["cum_pnl"] - s["peak"]
    return s


def per_city_calibration(df: pd.DataFrame, n_bins: int = 10) -> pd.DataFrame:
    """Brier + log loss + n per city. Uses model yes_probability vs outcome_yes."""
    out = []
    for city, g in df.dropna(subset=["yes_probability", "outcome_yes"]).groupby("city_abv"):
        if len(g) < 5:
            continue
        b = brier_score(g["yes_probability"], g["outcome_yes"])
        ll = log_loss(g["yes_probability"], g["outcome_yes"])
        d = brier_decomposition(g["yes_probability"].values, g["outcome_yes"].values, n_bins=n_bins)
        out.append({
            "city_abv": city, "n": int(len(g)),
            "brier": b, "log_loss": ll,
            "reliability": d["reliability"], "resolution": d["resolution"],
        })
    return pd.DataFrame(out).sort_values("brier")


def rolling_forecast_accuracy(df: pd.DataFrame, window_days: int = 7) -> pd.DataFrame:
    """7-day rolling MAE and bias on event-day forecasts.

    Input: per-(city, event_date) forecast_avg + actual_high. Aggregated into
    daily MAE/bias across all cities, then rolled window_days."""
    s = df.dropna(subset=["forecast_avg", "actual_high"]).copy()
    if len(s) == 0:
        return pd.DataFrame()
    s["event_date"] = pd.to_datetime(s["forecast_date"]).dt.date
    s["err"] = s["forecast_avg"] - s["actual_high"]
    s["abs_err"] = s["err"].abs()
    daily = (s.groupby("event_date")
              .agg(n=("err", "size"), mae=("abs_err", "mean"), bias=("err", "mean"))
              .reset_index().sort_values("event_date"))
    daily["mae_rolling"] = daily["mae"].rolling(window_days, min_periods=3).mean()
    daily["bias_rolling"] = daily["bias"].rolling(window_days, min_periods=3).mean()
    return daily


def position_cap_utilization(orders: pd.DataFrame, settlements: pd.DataFrame, cap: int = 500) -> pd.DataFrame:
    """Per-market cap utilization. Counts how often the bot's filled position
    approaches or exceeds the configured max_contracts cap. Useful to detect
    leaks (>cap) and slack (<<cap)."""
    if "position_no" not in settlements.columns:
        return pd.DataFrame()
    s = settlements[["market_ticker", "city_abv", "position_no"]].copy()
    s["position_no"] = s["position_no"].fillna(0).astype(int)
    s["util"] = s["position_no"] / float(cap)
    s["bucket"] = pd.cut(s["util"],
                        bins=[-0.001, 0.0, 0.25, 0.50, 0.80, 0.95, 1.00, 10.0],
                        labels=["0% (no fills)", "0-25%", "25-50%", "50-80%",
                                "80-95%", "95-100% (at cap)", ">100% (LEAK)"])
    return s
