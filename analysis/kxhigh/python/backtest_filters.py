"""Two structural filter backtests:

  1. Bucket-distance filter: only trade markets where the bucket center is
     ≥ X°F from the forecast μ. Tests "skip centered buckets" — the
     structural insight from the 4/23 loss post-mortem (centered buckets
     have high P(yes), so betting NO at hi_no_config overpays).

  2. Forecast-source agreement filter: only trade markets where NWS and
     WU agree within ≤ Y°F. Tests whether large source disagreement is
     a signal that the forecast is unreliable.

Uses bias-corrected NBM as the forecast μ when snapshots are missing
(4/12-4/18 window). For analysis #2, restricted to fills on snapshot
days where both NWS and WU were captured (4/19+).
"""
from pathlib import Path
import json
import numpy as np
import pandas as pd
from scipy.stats import norm

OUT = Path(__file__).resolve().parents[1] / "output"

CITY_NAME = {
    "AUS": "Austin", "MIA": "Miami", "THOU": "Houston", "DEN": "Denver",
    "NY": "New York City", "PHIL": "Philadelphia", "CHI": "Chicago",
    "LAX": "Los Angeles", "TATL": "Atlanta", "TDC": "Washington DC",
    "TPHX": "Phoenix", "TDAL": "Dallas", "TLV": "Las Vegas",
    "TOKC": "Oklahoma City", "TSEA": "Seattle", "TSFO": "San Francisco",
    "TSATX": "San Antonio", "TMIN": "Minneapolis", "TNOLA": "New Orleans",
    "TBOS": "Boston",
}


def load():
    fills = pd.DataFrame(json.loads((OUT / "fills_14d.json").read_text()))
    markets = pd.DataFrame(json.loads((OUT / "markets_meta.json").read_text()))
    snap_raw = json.loads((OUT / "snap_forecast.json").read_text())
    bias = json.loads((OUT / "nbm_bias_per_city.json").read_text())

    markets["bucket_val"] = pd.to_numeric(markets["bucket_val"])
    markets["mu_nbm"] = pd.to_numeric(markets["mu_nbm"])
    markets["mu_gfs"] = pd.to_numeric(markets["mu_gfs"])
    fills["contracts"] = pd.to_numeric(fills["contracts"])
    fills["no_price"] = pd.to_numeric(fills["no_price"])
    fills["fee"] = pd.to_numeric(fills["fee"])

    bias_map = {b["abv"]: float(b["bias_nws_minus_nbm"]) for b in bias}
    markets["city"] = markets["abv_raw"].map(CITY_NAME)
    # Bias-corrected NBM as best estimate of "what NWS+WU would have shown"
    markets["mu_corrected"] = markets["mu_nbm"] + markets["abv_raw"].map(bias_map).fillna(0)

    # Snapshot-derived NWS, WU per (city, event_date) — only for 4/19+
    snap_records = []
    for r in snap_raw:
        s = r["s"]
        snap_records.append({
            "city": r["city"],
            "event_date": r["event_date"],
            "nws": float(s["nws"]) if s.get("nws") is not None else None,
            "wu": float(s["wu"]) if s.get("wu") is not None else None,
            "snap_mu": float(s["mu"]) if s.get("mu") is not None else None,
        })
    snap_df = pd.DataFrame(snap_records)
    markets = markets.merge(snap_df, on=["city", "event_date"], how="left")

    # Use snapshot mu where present, else bias-corrected NBM
    markets["mu"] = markets["snap_mu"].fillna(markets["mu_corrected"])

    # Bucket reference point (where the bot's "if actual lands here, NO loses")
    # B markets: bucket center = bucket_val (e.g., B79.5 → 79.5, hits if actual ∈ {79, 80})
    # T markets (high tails): threshold = bucket_val + 0.5
    markets["bucket_ref"] = np.where(
        markets["bucket_type"] == "B",
        markets["bucket_val"],
        markets["bucket_val"] + 0.5,
    )
    markets["distance_from_mu"] = (markets["bucket_ref"] - markets["mu"]).abs()
    markets["nws_wu_diff"] = (markets["nws"] - markets["wu"]).abs()

    df = fills.merge(markets[[
        "market_ticker", "city", "abv_raw", "bucket_type", "bucket_val",
        "mu", "bucket_ref", "distance_from_mu", "nws_wu_diff",
        "nws", "wu",
    ]], on="market_ticker", how="left")
    df = df[df["outcome"].isin(["NO", "YES"])].copy()
    df["pnl"] = np.where(
        df["outcome"] == "NO",
        df["contracts"] * (1 - df["no_price"]) - df["fee"],
        -df["contracts"] * df["no_price"] - df["fee"],
    )
    df["fill_price_c"] = df["no_price"] * 100
    return df


def bootstrap_ci(deltas, iters=1000, seed=42):
    rng = np.random.default_rng(seed)
    n = len(deltas)
    if n == 0:
        return 0.0, 0.0
    boots = np.array([rng.choice(deltas, size=n, replace=True).sum() for _ in range(iters)])
    return tuple(np.percentile(boots, [2.5, 97.5]))


def run_distance_filter(df):
    print("=" * 90)
    print("ANALYSIS #1 — Bucket-distance filter")
    print("(Only trade markets where |bucket center − μ| ≥ threshold)")
    print("=" * 90)
    print(f"Sample: {len(df)} fills | with distance computed: {df['distance_from_mu'].notna().sum()}")
    control_pnl = df["pnl"].sum()
    print(f"Control P&L: ${control_pnl:.2f}\n")
    print(f"{'min Δ°F':>8s} {'fills_kept':>11s} {'kept_pnl':>10s} {'Δ vs ctrl':>11s} {'95% CI':>30s}")
    print("-" * 80)
    for thresh in [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]:
        keep = (df["distance_from_mu"] >= thresh) | df["distance_from_mu"].isna()
        kept_pnl = df.loc[keep, "pnl"].sum()
        per_market = df.assign(_pnl_filt=np.where(keep, df["pnl"], 0)).groupby("market_ticker").agg(
            ctrl=("pnl", "sum"), filt=("_pnl_filt", "sum"))
        per_market["delta"] = per_market["filt"] - per_market["ctrl"]
        lo, hi = bootstrap_ci(per_market["delta"].values)
        ci = f"[{lo:>+8.2f}, {hi:>+8.2f}]"
        sig = " *" if (lo > 0 or hi < 0) else "  "
        print(f"{thresh:>8.1f} {keep.sum():>11d} {kept_pnl:>10.2f} {kept_pnl-control_pnl:>+11.2f} {ci:>30s}{sig}")
    print()


def run_distance_filter_per_city(df):
    print("=" * 90)
    print("Per-city impact of distance-filter at threshold = 1.0°F")
    print("=" * 90)
    thresh = 1.0
    keep = (df["distance_from_mu"] >= thresh) | df["distance_from_mu"].isna()
    df = df.assign(_pnl_filt=np.where(keep, df["pnl"], 0))
    per_city = df.groupby("city").agg(
        n=("market_ticker", "count"),
        ctrl=("pnl", "sum"),
        filt=("_pnl_filt", "sum"),
    )
    per_city["delta"] = per_city["filt"] - per_city["ctrl"]
    print(per_city.sort_values("delta").round(2).to_string())
    print()


def run_agreement_filter(df):
    print("=" * 90)
    print("ANALYSIS #2 — NWS/WU agreement filter")
    print("(Only trade markets where |NWS − WU| ≤ threshold; restricted to")
    print("fills on snapshot-coverage days where both sources were captured)")
    print("=" * 90)
    df_with_both = df[df["nws"].notna() & df["wu"].notna()].copy()
    if df_with_both.empty:
        print("No fills with both NWS and WU captured.\n")
        return
    print(f"Sample: {len(df_with_both)} fills (subset of total {len(df)} where both sources available)")
    control_pnl_subset = df_with_both["pnl"].sum()
    full_control = df["pnl"].sum()
    print(f"Subset control P&L: ${control_pnl_subset:.2f}  (full 14d: ${full_control:.2f})\n")
    print(f"{'max diff':>8s} {'fills_kept':>11s} {'kept_pnl':>10s} {'Δ vs subset_ctrl':>17s} {'95% CI':>30s}")
    print("-" * 90)
    # Sub-control = "if we'd applied filter on this subset only"
    for thresh in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
        keep_sub = df_with_both["nws_wu_diff"] <= thresh
        kept_pnl = df_with_both.loc[keep_sub, "pnl"].sum()
        per_market = df_with_both.assign(
            _pnl_filt=np.where(keep_sub, df_with_both["pnl"], 0)
        ).groupby("market_ticker").agg(ctrl=("pnl", "sum"), filt=("_pnl_filt", "sum"))
        per_market["delta"] = per_market["filt"] - per_market["ctrl"]
        lo, hi = bootstrap_ci(per_market["delta"].values)
        ci = f"[{lo:>+8.2f}, {hi:>+8.2f}]"
        sig = " *" if (lo > 0 or hi < 0) else "  "
        print(f"{thresh:>8.1f} {int(keep_sub.sum()):>11d} {kept_pnl:>10.2f} "
              f"{kept_pnl-control_pnl_subset:>+17.2f} {ci:>30s}{sig}")
    print()
    # Distribution of |NWS-WU|
    print(f"Distribution of |NWS − WU| in available fills:")
    diffs = df_with_both["nws_wu_diff"].values
    for q in [0.25, 0.50, 0.75, 0.90, 0.95]:
        print(f"  {int(q*100):>3d}th pct: {np.quantile(diffs, q):.2f}°F")


def main():
    df = load()
    run_distance_filter(df)
    run_distance_filter_per_city(df)
    run_agreement_filter(df)


if __name__ == "__main__":
    main()
