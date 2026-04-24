"""Build 20-city forecast-error distribution from GFS+ECMWF historical
forecasts vs NWS CLI actuals, and compare to the hardcoded 8-city table
in high_temp_trading.py:911-924.

Rolling 90-day window ending on the most recent CLI reading date.

Output:
  - analysis/kxhigh/output/forecast_error_dist_20city.csv  (computed table)
  - stdout: side-by-side comparison for the 8 existing cities
"""
import json
import sys
from pathlib import Path
import pandas as pd

RAW = Path(__file__).resolve().parents[1] / "output" / "dist_raw.json"
OUT_CSV = Path(__file__).resolve().parents[1] / "output" / "forecast_error_dist_20city.csv"
MIN_SAMPLES = 60

HARDCODED_CITIES = ["Austin", "Miami", "Denver", "Houston",
                    "Philadelphia", "New York City", "Chicago", "Los Angeles"]
HARDCODED_ROWS = ["5", "4", "3", "2", "1", "0", "-1", "-2", "-3", "-4", "-5"]
HARDCODED_DATA = [
    [0.0270, 0.0256, 0.1290, 0.0000, 0.0000, 0.0270, 0.0333, 0.0667],
    [0.0270, 0.0000, 0.0000, 0.0000, 0.0263, 0.0000, 0.0333, 0.0000],
    [0.0811, 0.0000, 0.0645, 0.1613, 0.1579, 0.1081, 0.1333, 0.0000],
    [0.2432, 0.2308, 0.1290, 0.1935, 0.1053, 0.1622, 0.2333, 0.0000],
    [0.1892, 0.2051, 0.1935, 0.1935, 0.2895, 0.2162, 0.3333, 0.2667],
    [0.1351, 0.2821, 0.1935, 0.0645, 0.1053, 0.2162, 0.0667, 0.2667],
    [0.1892, 0.1282, 0.0645, 0.2258, 0.1053, 0.0811, 0.1000, 0.1333],
    [0.0270, 0.0769, 0.0645, 0.0968, 0.1053, 0.1622, 0.0000, 0.1333],
    [0.0541, 0.0256, 0.0968, 0.0323, 0.0526, 0.0000, 0.0000, 0.0667],
    [0.0000, 0.0000, 0.0323, 0.0000, 0.0263, 0.0000, 0.0333, 0.0000],
    [0.0270, 0.0256, 0.0323, 0.0323, 0.0263, 0.0270, 0.0333, 0.0667],
]


def load_raw():
    if not RAW.exists():
        sys.exit(f"Run the BQ query first; {RAW} not found.")
    rows = json.loads(RAW.read_text())
    df = pd.DataFrame(rows)
    df["variation"] = df["variation"].astype(int)
    df["n"] = df["n"].astype(int)
    return df


def build_dist(df):
    """Raw (no smoothing) probabilities per (city, variation). Fill missing
    variations in [-5, 5] with 0 so every column has all 11 rows."""
    pivot = df.pivot_table(
        index="variation", columns="city", values="n", aggfunc="sum", fill_value=0
    )
    # Ensure all 11 variation levels present
    for v in range(-5, 6):
        if v not in pivot.index:
            pivot.loc[v] = 0
    pivot = pivot.sort_index(ascending=False)  # +5 at top, -5 at bottom
    # Normalize per city column
    sample_size = pivot.sum(axis=0)
    probs = pivot.div(sample_size, axis=1).fillna(0)
    return probs, sample_size


def main():
    df = load_raw()
    probs, sample_size = build_dist(df)

    # Reformat index as string "5" ... "-5" to match hardcoded convention
    probs.index = probs.index.astype(int).astype(str)

    # Write CSV
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    probs.to_csv(OUT_CSV)
    print(f"Wrote {OUT_CSV}")
    print(f"  Cities: {list(probs.columns)}")
    print()

    # Sample sizes
    print("Sample sizes (rolling 90 days):")
    for city in sorted(probs.columns, key=lambda c: -sample_size[c]):
        tag = "" if sample_size[city] >= MIN_SAMPLES else "  <-- BELOW 60"
        print(f"  {city:<20s}  n={sample_size[city]:>3d}{tag}")
    print()

    # Build hardcoded table as DataFrame for comparison
    hc = pd.DataFrame(HARDCODED_DATA, index=HARDCODED_ROWS, columns=HARDCODED_CITIES)

    # Side-by-side comparison per existing city
    print("=" * 90)
    print("COMPARISON: hardcoded (HC) vs rebuilt-from-GFS+ECMWF (NEW) for existing 8 cities")
    print("Rows: round(actual - forecast), clipped [-5, +5]")
    print("=" * 90)

    summary_rows = []
    for city in HARDCODED_CITIES:
        if city not in probs.columns:
            print(f"\n{city}: NOT in rebuilt table — skipping")
            continue
        hc_col = hc[city]
        new_col = probs[city].reindex(HARDCODED_ROWS).fillna(0)

        print(f"\n{city}  (HC n≈37 | NEW n={sample_size[city]}):")
        print(f"  {'Δ°F':>4s}  {'HC%':>7s}   {'NEW%':>7s}   diff    bar")
        for r in HARDCODED_ROWS:
            h = hc_col[r]
            n = new_col[r]
            d = n - h
            bar_hc = "#" * int(h * 50)
            bar_new = "*" * int(n * 50)
            print(f"  {r:>4s}  {h*100:>6.1f}%  {n*100:>6.1f}%  {d*100:+6.1f}%  HC:{bar_hc:<15s} NEW:{bar_new}")

        # Summary stats: mean, sd, mode
        variations = [int(r) for r in HARDCODED_ROWS]
        hc_mean = sum(v * p for v, p in zip(variations, hc_col))
        new_mean = sum(v * p for v, p in zip(variations, new_col))
        hc_var = sum(((v - hc_mean) ** 2) * p for v, p in zip(variations, hc_col))
        new_var = sum(((v - new_mean) ** 2) * p for v, p in zip(variations, new_col))
        hc_sd = hc_var ** 0.5
        new_sd = new_var ** 0.5
        # Total variation distance
        tvd = sum(abs(h - n) for h, n in zip(hc_col, new_col)) / 2
        summary_rows.append({
            "city": city, "n_new": int(sample_size[city]),
            "hc_mean": round(hc_mean, 2), "new_mean": round(new_mean, 2),
            "mean_shift": round(new_mean - hc_mean, 2),
            "hc_sd": round(hc_sd, 2), "new_sd": round(new_sd, 2),
            "tvd": round(tvd, 3),
        })

    print()
    print("=" * 90)
    print("SUMMARY STATS per city (HC=hardcoded, NEW=rebuilt)")
    print("=" * 90)
    sdf = pd.DataFrame(summary_rows)
    print(sdf.to_string(index=False))
    print()
    print("mean_shift = NEW mean − HC mean (positive = GFS errors warmer-biased than HC)")
    print("tvd        = total variation distance between distributions (0 = identical, 1 = disjoint)")

    # New cities
    new_cities = [c for c in probs.columns if c not in HARDCODED_CITIES]
    print()
    print("=" * 90)
    print(f"NEW CITIES now available ({len(new_cities)}):")
    print("=" * 90)
    for city in sorted(new_cities):
        tag = "" if sample_size[city] >= MIN_SAMPLES else "  <-- BELOW 60, fallback to static"
        print(f"  {city:<20s}  n={sample_size[city]:>3d}{tag}")


if __name__ == "__main__":
    main()
