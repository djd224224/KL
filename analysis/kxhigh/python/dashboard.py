"""Build interactive HTML dashboard for KXHIGH model + execution validation."""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent))
import load as loader
import metrics as M

OUTPUT = Path(__file__).resolve().parents[1] / "output" / "kxhigh_validation.html"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)


# ---------- helpers ----------
def section(title: str, subtitle: str = "") -> str:
    sub = f'<p class="sub">{subtitle}</p>' if subtitle else ""
    return f'<div class="section"><h2>{title}</h2>{sub}'


def section_end() -> str:
    return "</div>"


def fig_to_html(fig) -> str:
    return fig.to_html(include_plotlyjs="cdn", full_html=False, div_id=None)


def table_html(df: pd.DataFrame, max_rows: int = 30, float_fmt: str = "{:.3f}") -> str:
    if df is None or len(df) == 0:
        return '<p class="muted">No data.</p>'
    disp = df.head(max_rows).copy()
    for c in disp.select_dtypes(include=[float]).columns:
        disp[c] = disp[c].map(lambda x: "" if pd.isna(x) else float_fmt.format(x))
    return disp.to_html(index=False, classes="tbl", border=0, escape=True)


# ---------- build modules ----------
def build_sanity(data: dict) -> str:
    resolved = data["resolved"]
    fills = data["fills"]
    orders = data["orders"]
    settle = data["settlements"]
    snap = data["snapshots"]

    snap_dates = pd.to_datetime(snap["run_date"], errors="coerce", utc=True).dt.date
    fill_dates = pd.to_datetime(fills["fill_ts"], errors="coerce", utc=True).dt.date

    cards = [
        ("Snapshots (rows)", f"{len(snap):,}", f"days: {snap_dates.nunique()}, "
         f"{snap_dates.min()} → {snap_dates.max()}"),
        ("Fills (rows)", f"{len(fills):,}", f"days: {fill_dates.nunique()}"),
        ("Orders", f"{len(orders):,}",
         f"filled: {int(orders['any_fill'].sum()):,} "
         f"({orders['any_fill'].mean():.1%})"),
        ("Settlements", f"{len(settle):,}",
         f"YES: {(settle['result']=='YES').sum()}, NO: {(settle['result']=='NO').sum()}"),
        ("Resolved-with-snapshot", f"{len(resolved):,}",
         f"⚠ limited by snapshot gap"),
        ("Total P&L (settled)", f"${settle['pnl'].sum():,.2f}", ""),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v">{v}</div><div class="d">{d}</div></div>'
        for k, v, d in cards
    )

    caveat = ('<div class="caveat">⚠️ <b>Snapshot coverage limited to Mar 9–10, 2026</b> '
              '(864 rows; only 19 markets have both snapshot + settlement). '
              'Model-validation sections below are thin — extend snapshot logging '
              'to run every cycle for more robust analysis. '
              '<br>AccuWeather source is 100% NULL (API key expired Mar 2026).</div>')
    return f'<div class="cards">{card_html}</div>{caveat}'


def build_forecast_accuracy(resolved: pd.DataFrame) -> str:
    if len(resolved) == 0:
        return '<p class="muted">No resolved-with-snapshot rows.</p>'
    df = resolved.dropna(subset=["actual_high_estimate"]).copy()
    if len(df) == 0:
        return '<p class="muted">No markets with an inferred actual_high.</p>'

    # Scatter forecast_avg vs actual_high
    fig1 = px.scatter(
        df, x="forecast_avg", y="actual_high_estimate",
        color="city", hover_data=["market_ticker", "forecast_error"],
        title="Forecast avg vs actual high estimate",
    )
    lo = min(df["forecast_avg"].min(), df["actual_high_estimate"].min()) - 2
    hi = max(df["forecast_avg"].max(), df["actual_high_estimate"].max()) + 2
    fig1.add_shape(type="line", x0=lo, y0=lo, x1=hi, y1=hi,
                   line=dict(dash="dash", color="gray"))
    fig1.update_layout(height=500)

    # Per-source accuracy
    acc = M.forecast_accuracy(df)
    acc_all = acc[acc["city"] == "__all__"].copy()
    acc_city = acc[acc["city"] != "__all__"].copy()

    fig2 = None
    if len(acc_city) > 0:
        fig2 = px.bar(acc_city, x="city", y="mae", color="source", barmode="group",
                      title="MAE per source × city")
        fig2.update_layout(height=450)

    # Residual per city
    df["residual"] = df["forecast_avg"] - df["actual_high_estimate"]
    fig3 = px.box(df, x="city", y="residual",
                  title="Forecast residuals (forecast_avg − actual) by city")
    fig3.add_hline(y=0, line_dash="dash", line_color="gray")
    fig3.update_layout(height=450)

    html = fig_to_html(fig1)
    html += "<h3>Source accuracy (overall)</h3>" + table_html(acc_all)
    if fig2 is not None:
        html += fig_to_html(fig2)
    html += fig_to_html(fig3)
    return html


def build_calibration(resolved: pd.DataFrame) -> str:
    if len(resolved) == 0:
        return '<p class="muted">No resolved-with-snapshot rows.</p>'
    df = resolved.dropna(subset=["yes_probability", "outcome_yes"]).copy()
    if len(df) < 5:
        return '<p class="muted">Insufficient resolved markets for calibration '
        f'(need ≥5, have {len(df)}).</p>'

    # Model metrics
    b_model = M.brier_score(df["yes_probability"], df["outcome_yes"])
    ll_model = M.log_loss(df["yes_probability"], df["outcome_yes"])
    decomp = M.brier_decomposition(df["yes_probability"], df["outcome_yes"], n_bins=10)

    # Market-implied (from snapshot) for comparison
    mkt_mask = df["market_implied_yes_prob_snap"].notna()
    b_mkt = M.brier_score(df.loc[mkt_mask, "market_implied_yes_prob_snap"],
                          df.loc[mkt_mask, "outcome_yes"])

    metrics_html = (
        '<div class="cards">'
        f'<div class="card"><div class="k">Brier (model)</div><div class="v">{b_model:.3f}</div>'
        f'<div class="d">n={decomp["n"]}</div></div>'
        f'<div class="card"><div class="k">Log loss (model)</div><div class="v">{ll_model:.3f}</div><div class="d"></div></div>'
        f'<div class="card"><div class="k">Brier (market-implied)</div><div class="v">{b_mkt:.3f}</div>'
        f'<div class="d">n={int(mkt_mask.sum())}</div></div>'
        f'<div class="card"><div class="k">Reliability</div><div class="v">{decomp["reliability"]:.4f}</div>'
        f'<div class="d">(lower better)</div></div>'
        f'<div class="card"><div class="k">Resolution</div><div class="v">{decomp["resolution"]:.4f}</div>'
        f'<div class="d">(higher better)</div></div>'
        f'<div class="card"><div class="k">Uncertainty</div><div class="v">{decomp["uncertainty"]:.4f}</div><div class="d"></div></div>'
        '</div>'
    )

    # Reliability diagram
    rel = M.reliability_bins(df["yes_probability"].values, df["outcome_yes"].values, n_bins=10)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines",
                             line=dict(dash="dash", color="gray"), name="Perfect"))
    fig.add_trace(go.Scatter(
        x=rel["mean_predicted"], y=rel["actual_yes_rate"],
        mode="markers+lines",
        marker=dict(size=rel["n"] * 2 + 4, color="steelblue"),
        name="Model",
        text=[f"n={n}" for n in rel["n"]], hoverinfo="x+y+text",
    ))
    fig.update_layout(title="Reliability diagram (model yes_prob vs actual)",
                      xaxis_title="Predicted P(YES)", yaxis_title="Observed YES rate",
                      height=500)

    return metrics_html + fig_to_html(fig) + "<h3>Bin breakdown</h3>" + table_html(rel)


def build_spread_vs_pnl(resolved: pd.DataFrame) -> str:
    if len(resolved) == 0 or resolved["pnl"].isna().all():
        return '<p class="muted">No data.</p>'

    html_parts = []
    for col, label in [("forecast_std", "forecast_std"),
                       ("forecast_std_recomputed", "forecast_std (recomputed, NWS+WU)"),
                       ("forecast_range", "forecast_range")]:
        if col not in resolved.columns:
            continue
        binned = M.spread_vs_pnl(resolved, col, n_bins=4)
        if len(binned) == 0:
            continue
        binned["bin_label"] = binned["bin"].astype(str)
        fig = make_subplots(rows=1, cols=2, subplot_titles=("Mean P&L by bin", "Win rate by bin"))
        fig.add_trace(go.Bar(x=binned["bin_label"], y=binned["mean_pnl"],
                             marker_color="steelblue", text=binned["n"],
                             texttemplate="n=%{text}"), row=1, col=1)
        fig.add_trace(go.Bar(x=binned["bin_label"], y=binned["win_rate"],
                             marker_color="seagreen"), row=1, col=2)
        fig.update_layout(title=f"{label}: spread vs P&L", height=400, showlegend=False)
        html_parts.append(f'<h3>{label}</h3>' + fig_to_html(fig))
        html_parts.append(table_html(binned.drop(columns=["bin"])))
    return "".join(html_parts) or '<p class="muted">No usable spread columns.</p>'


def build_edge_capture(fills: pd.DataFrame) -> str:
    df = fills.dropna(subset=["model_edge_at_fill", "realized_pnl_per_fill"]).copy()
    if len(df) == 0:
        return '<p class="muted">No fills have snapshot context (limited snapshot coverage).</p>'
    fig = px.scatter(df, x="model_edge_at_fill", y="realized_pnl_per_fill",
                     color="city", hover_data=["market_ticker", "fill_price"],
                     title=f"Model edge at fill vs realized P&L (n={len(df)})")
    fig.add_hline(y=0, line_dash="dash", line_color="gray")
    fig.add_vline(x=0, line_dash="dash", line_color="gray")
    fig.update_layout(height=500)
    # Edge-binned P&L
    df["edge_bin"] = pd.qcut(df["model_edge_at_fill"], q=5, duplicates="drop")
    edge_tbl = (df.groupby("edge_bin", observed=True)
                  .agg(n=("realized_pnl_per_fill", "size"),
                       mean_pnl=("realized_pnl_per_fill", "mean"),
                       total_pnl=("realized_pnl_per_fill", "sum"),
                       win_rate=("realized_pnl_per_fill", lambda x: (x > 0).mean()))
                  .reset_index())
    edge_tbl["edge_bin"] = edge_tbl["edge_bin"].astype(str)
    return fig_to_html(fig) + "<h3>P&L by edge quintile</h3>" + table_html(edge_tbl)


def build_execution(orders: pd.DataFrame, fills: pd.DataFrame) -> str:
    by_city = M.fill_rate_by(orders, "city_abv").head(25)
    fig1 = px.bar(by_city, x="city_abv", y="fill_rate_contracts",
                  title="Fill rate (contracts) by city")
    fig1.update_layout(height=400)

    # Fill rate by price bucket
    o = orders.copy()
    o["price_bucket"] = pd.cut(o["ordered_no_price_cents"],
                               bins=[0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    pb = (o.groupby("price_bucket", observed=True)
           .agg(n_orders=("client_order_id", "size"),
                n_filled=("any_fill", "sum"),
                total_ordered=("ordered_contracts", "sum"),
                total_filled=("filled_contracts", "sum"))
           .reset_index())
    pb["fill_rate"] = pb["total_filled"] / pb["total_ordered"].where(pb["total_ordered"] > 0, np.nan)
    pb["price_bucket"] = pb["price_bucket"].astype(str)
    fig2 = px.bar(pb, x="price_bucket", y="fill_rate",
                  title="Fill rate by limit NO-price bucket (cents)")
    fig2.update_layout(height=400)

    # Taker/maker + fee drag (from fills)
    f = fills.dropna(subset=["realized_pnl_per_fill"]).copy()
    total_gross = (f["realized_pnl_per_fill"] + f["fee_cost_dollars"]).sum()
    total_fees = f["fee_cost_dollars"].sum()
    total_net = f["realized_pnl_per_fill"].sum()
    taker = f[f["is_taker"] == True]
    maker = f[f["is_taker"] == False]
    exec_cards = "".join([
        f'<div class="card"><div class="k">Gross P&L (fills)</div><div class="v">${total_gross:,.2f}</div><div class="d"></div></div>',
        f'<div class="card"><div class="k">Fees</div><div class="v">${total_fees:,.2f}</div><div class="d">{total_fees/total_gross*100 if total_gross else 0:.2f}% of gross</div></div>',
        f'<div class="card"><div class="k">Net P&L (fills)</div><div class="v">${total_net:,.2f}</div><div class="d">vs ${total_gross - total_fees:,.2f} computed</div></div>',
        f'<div class="card"><div class="k">Taker fills</div><div class="v">{len(taker):,}</div><div class="d">P&L ${taker["realized_pnl_per_fill"].sum():,.2f}</div></div>',
        f'<div class="card"><div class="k">Maker fills</div><div class="v">{len(maker):,}</div><div class="d">P&L ${maker["realized_pnl_per_fill"].sum():,.2f}</div></div>',
    ])
    return (fig_to_html(fig1) + fig_to_html(fig2)
            + f'<div class="cards">{exec_cards}</div>'
            + "<h3>By city (top 25)</h3>" + table_html(by_city))


def build_pnl_attribution(settle: pd.DataFrame) -> str:
    df = settle.copy()
    # Parse settled_time
    df["settled_ts"] = pd.to_datetime(df["settled_time"], errors="coerce", utc=True)
    df = df.dropna(subset=["settled_ts"]).sort_values("settled_ts")
    df["cum_pnl"] = df["pnl"].cumsum()
    df["peak"] = df["cum_pnl"].cummax()
    df["drawdown"] = df["cum_pnl"] - df["peak"]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        subplot_titles=("Cumulative P&L", "Drawdown"))
    fig.add_trace(go.Scatter(x=df["settled_ts"], y=df["cum_pnl"],
                             mode="lines", line=dict(color="steelblue"),
                             name="Cum P&L"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df["settled_ts"], y=df["drawdown"],
                             mode="lines", line=dict(color="crimson"),
                             fill="tozeroy", name="Drawdown"), row=2, col=1)
    fig.update_layout(height=600, showlegend=False)

    # Per-city attribution
    df["city_code"] = df["city_code"].fillna("?")
    by_city = df.groupby("city_code").agg(
        n=("pnl", "size"), total_pnl=("pnl", "sum"),
        mean_pnl=("pnl", "mean"),
        win_rate=("pnl", lambda x: (x > 0).mean()),
        total_cost=("total_cost", "sum"),
    ).reset_index()
    by_city["roi"] = by_city["total_pnl"] / by_city["total_cost"].where(by_city["total_cost"] > 0, np.nan)
    by_city = by_city.sort_values("total_pnl", ascending=False)
    fig2 = px.bar(by_city, x="city_code", y="total_pnl",
                  color="roi", color_continuous_scale="RdYlGn",
                  hover_data=["n", "win_rate"], title="Total P&L by city")
    fig2.update_layout(height=450)

    # Per-day P&L
    df["date"] = df["settled_ts"].dt.date
    daily = df.groupby("date").agg(pnl=("pnl", "sum"), n=("pnl", "size")).reset_index()
    fig3 = px.bar(daily, x="date", y="pnl", hover_data=["n"],
                  title="Daily P&L")
    fig3.update_layout(height=350)

    # Weekday
    df["weekday"] = df["settled_ts"].dt.day_name()
    wd_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    wd = df.groupby("weekday").agg(total_pnl=("pnl", "sum"), n=("pnl", "size")).reindex(wd_order).reset_index()
    fig4 = px.bar(wd, x="weekday", y="total_pnl", hover_data=["n"],
                  title="P&L by weekday")
    fig4.update_layout(height=350)

    return (fig_to_html(fig) + fig_to_html(fig2) + fig_to_html(fig3) + fig_to_html(fig4)
            + "<h3>Per-city table</h3>" + table_html(by_city))


CSS = """
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1400px; margin: 0 auto; padding: 24px; color: #222; background: #fafafa; }
h1 { border-bottom: 3px solid #333; padding-bottom: 8px; }
h2 { margin-top: 48px; color: #1a1a1a; border-left: 4px solid steelblue; padding-left: 12px; }
h3 { margin-top: 24px; color: #444; font-size: 15px; }
.section { background: white; padding: 24px; border-radius: 8px; margin-bottom: 24px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
.sub { color: #666; font-size: 13px; margin-top: -4px; }
.muted { color: #888; font-style: italic; }
.caveat { background: #fffaf0; border-left: 4px solid #ff9800; padding: 12px 16px;
          margin: 16px 0; border-radius: 4px; font-size: 13px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 12px; margin: 16px 0; }
.card { background: white; border: 1px solid #e0e0e0; border-radius: 6px; padding: 12px; }
.card .k { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
.card .v { font-size: 22px; font-weight: 600; color: #1a1a1a; margin-top: 4px; }
.card .d { font-size: 11px; color: #888; margin-top: 2px; }
.tbl { border-collapse: collapse; width: 100%; font-size: 13px; margin: 8px 0; }
.tbl th, .tbl td { padding: 6px 10px; text-align: right; border-bottom: 1px solid #eee; }
.tbl th { background: #f5f5f5; font-weight: 600; }
.tbl tr:hover { background: #f9f9f9; }
.toc { background: #f0f4f8; padding: 12px 16px; border-radius: 6px; margin: 16px 0; font-size: 13px; }
.toc a { color: steelblue; text-decoration: none; margin-right: 16px; }
</style>
"""


def main():
    print("Loading data from BigQuery...")
    data = loader.load_all()
    for name, df in data.items():
        print(f"  {name}: {len(df):,} rows")

    modules = [
        ("overview", "Overview & Sanity", build_sanity(data)),
        ("forecast_accuracy", "1 · Forecast accuracy", build_forecast_accuracy(data["resolved"])),
        ("calibration", "2 · Prediction calibration",
         build_calibration(data["resolved"])),
        ("spread_pnl", "3 · Forecast spread ↔ P&L",
         build_spread_vs_pnl(data["resolved"])),
        ("edge_capture", "4 · Edge capture (per fill)",
         build_edge_capture(data["fills"])),
        ("execution", "5 · Execution quality",
         build_execution(data["orders"], data["fills"])),
        ("pnl", "6 · P&L attribution",
         build_pnl_attribution(data["settlements"])),
    ]

    toc = '<div class="toc"><b>Jump to:</b> ' + " · ".join(
        f'<a href="#{mid}">{title}</a>' for mid, title, _ in modules
    ) + "</div>"

    built_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = "".join(
        f'<div id="{mid}">{section(title)}{content}{section_end()}</div>'
        for mid, title, content in modules
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>KXHIGH validation</title>
{CSS}</head>
<body>
<h1>KXHIGH model + execution validation</h1>
<p class="sub">Built {built_ts} · source: BigQuery views <code>KXHIGH_*</code> in <code>elite-contact-446323-q7.Kalshi</code></p>
{toc}
{body}
</body></html>"""

    OUTPUT.write_text(html, encoding="utf-8")
    print(f"\nWrote {OUTPUT} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
