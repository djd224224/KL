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


def exec_summary(text: str) -> str:
    """Static 'what this section measures' block at the top."""
    return f'<div class="exec">{text}</div>'


def commentary(bullets: list[str], headline: str = "") -> str:
    """Data-driven takeaways block at the bottom of each section.
    Rendered with a distinct style so the reader knows it's auto-generated
    from this run's numbers (not boilerplate). Pass a list of 3-5 short
    plain-English findings."""
    head = f'<div class="ctl">Takeaways</div>'
    items = "".join(f"<li>{b}</li>" for b in bullets if b)
    headline_html = f'<div class="chd">{headline}</div>' if headline else ""
    return f'<div class="cmt">{head}{headline_html}<ul>{items}</ul></div>'


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

    total_pnl = settle["pnl_dollars"].sum()
    n_days = fill_dates.nunique()
    win_rate = (settle["pnl_dollars"] > 0).mean()
    summary = (
        '<span class="tl">What this dashboard answers</span>'
        f'Our bot places bets on daily high-temperature markets across 19 US cities. '
        f'Over {n_days} trading days we settled {len(settle)} markets for '
        f'<b>${total_pnl:,.0f} total P&L</b> ({win_rate:.0%} of markets profitable). '
        f'Below, six sections grade (1) how well we forecast the weather, '
        f'(2) how well that forecast translates into accurate probability calls, '
        f'(3) whether we make money when forecasts disagree, '
        f'(4) whether the model&rsquo;s perceived "edge" at each trade turns into real profit, '
        f'(5) whether our order execution is efficient, and '
        f'(6) where the money is actually coming from (which cities, which days).'
    )

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
        ("Total P&L (settled)", f"${settle['pnl_dollars'].sum():,.2f}", ""),
        ("Winning temp available", f"{settle['winning_high_temp'].notna().sum()}/{len(settle)}",
         "from between-bucket midpoints"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v">{v}</div><div class="d">{d}</div></div>'
        for k, v, d in cards
    )

    n_resolved_with_snap = int(resolved["forecast_avg"].notna().sum())
    caveat = (f'<div class="caveat">⚠️ <b>{n_resolved_with_snap} of {len(settle)} settled markets '
              f'have a bot snapshot</b> — Mar 11 → Apr 18 had a silent snapshot-logging '
              f'outage (fixed). Model-validation sections below are limited to those rows. '
              f'<br>AccuWeather source is 100% NULL (API key expired Mar 2026).</div>')

    # Dynamic commentary based on this run
    settle_pnl = settle["pnl_dollars"].sum()
    n_win = (settle["pnl_dollars"] > 0).sum()
    n_loss = (settle["pnl_dollars"] < 0).sum()
    win_rate = n_win / len(settle) if len(settle) else 0
    roi = settle_pnl / settle["total_cost_dollars"].sum() if settle["total_cost_dollars"].sum() > 0 else 0
    n_days = fill_dates.nunique()
    daily_pnl = settle_pnl / n_days if n_days else 0
    avg_markets_per_day = len(settle) / n_days if n_days else 0
    fill_rate = orders["any_fill"].mean()

    verdict = "profitable" if settle_pnl > 0 else ("break-even" if abs(settle_pnl) < 50 else "losing money")
    win_comment = "robust" if win_rate > 0.6 else ("marginal" if win_rate > 0.5 else "poor")

    takeaways = [
        f"Over {n_days} trading days the bot is <b>{verdict}</b>: "
        f"<b>${settle_pnl:,.0f}</b> net P&L on <b>${settle['total_cost_dollars'].sum():,.0f}</b> "
        f"deployed capital (<b>{roi:.1%}</b> ROI).",
        f"Win rate is {win_comment}: <b>{win_rate:.0%}</b> of {len(settle)} settled markets "
        f"profitable ({n_win} winners vs {n_loss} losers). Averaging {avg_markets_per_day:.0f} "
        f"markets settled per trading day.",
        f"Daily P&L averages <b>${daily_pnl:+,.0f}/day</b>. Fill rate on placed orders is "
        f"<b>{fill_rate:.1%}</b> (low by design — bot is post-only).",
        f"⚠ <b>Model-validation sections (2, 3, 4) are limited to {n_resolved_with_snap} settled markets</b> "
        f"with bot snapshots due to the Mar 11–Apr 18 snapshot logging outage. The silent "
        f"failure is fixed going forward; give it ~2 more weeks of runs before revisiting.",
    ]

    return exec_summary(summary) + commentary(takeaways) + f'<div class="cards">{card_html}</div>{caveat}'


def build_forecast_accuracy(resolved: pd.DataFrame) -> str:
    summary = (
        '<span class="tl">What this section measures</span>'
        'How close our weather forecasts came to the actual high temperature in each city. '
        'We aggregate forecasts from the National Weather Service (NWS) and Weather Underground '
        '(AccuWeather is dead — API key expired). '
        '<b>MAE</b> (mean absolute error) = typical miss in &deg;F. '
        '<b>Bias</b> = systematic over/under forecast (near zero is ideal). '
        'Cities where our forecast is consistently off are cities where betting is riskier.'
    )
    if len(resolved) == 0:
        return exec_summary(summary) + '<p class="muted">No resolved markets.</p>'
    df = resolved.dropna(subset=["actual_high_estimate", "forecast_avg"]).copy()
    if len(df) == 0:
        return exec_summary(summary) + '<p class="muted">No markets with both forecast and actual.</p>'

    # Aggregate to unique (city, event_date) pairs so we don't over-weight events
    # with many buckets (each event has ~18 markets sharing the same forecast/actual).
    unique_days = (
        df.groupby(["city_abv", "forecast_date"], as_index=False)
          .agg(
              forecast_avg=("forecast_avg", "mean"),
              actual_high=("actual_high_estimate", "mean"),
              forecast_source=("forecast_source", "first"),
          )
    )
    unique_days["residual"] = unique_days["forecast_avg"] - unique_days["actual_high"]

    # Scatter
    fig1 = px.scatter(
        unique_days, x="forecast_avg", y="actual_high",
        color="city_abv", symbol="forecast_source",
        hover_data=["forecast_date", "residual"],
        title=f"Forecast avg vs actual high — per event-day (n={len(unique_days)})",
    )
    lo = min(unique_days["forecast_avg"].min(), unique_days["actual_high"].min()) - 2
    hi = max(unique_days["forecast_avg"].max(), unique_days["actual_high"].max()) + 2
    fig1.add_shape(type="line", x0=lo, y0=lo, x1=hi, y1=hi,
                   line=dict(dash="dash", color="gray"))
    fig1.update_layout(height=500)

    # MAE/bias per city using the bot's ensemble forecast (NWS + WU snapshot)
    rows = []
    for (city, g) in unique_days.groupby("city_abv"):
        s = g[["forecast_avg", "actual_high"]].dropna()
        if len(s) == 0:
            continue
        err = s["forecast_avg"] - s["actual_high"]
        rows.append({
            "city_abv": city, "source": "ensemble", "n": len(s),
            "mae": err.abs().mean(), "bias": err.mean(),
            "rmse": float(np.sqrt((err ** 2).mean())),
        })
    acc_city = pd.DataFrame(rows)

    # Overall across all cities
    all_rows = []
    s = unique_days[["forecast_avg", "actual_high"]].dropna()
    if len(s) > 0:
        err = s["forecast_avg"] - s["actual_high"]
        all_rows.append({
            "source": "ensemble", "n": len(s),
            "mae": err.abs().mean(), "bias": err.mean(),
            "rmse": float(np.sqrt((err ** 2).mean())),
        })
    acc_all = pd.DataFrame(all_rows)

    fig2 = px.bar(acc_city, x="city_abv", y="mae", color="source", barmode="group",
                  title="MAE by city (°F)")
    fig2.update_layout(height=450)

    fig3 = px.box(unique_days, x="city_abv", y="residual",
                  title="Forecast residuals by city (forecast − actual, °F)")
    fig3.add_hline(y=0, line_dash="dash", line_color="gray")
    fig3.update_layout(height=450)

    n_snap_evt = int((unique_days["forecast_source"] == "snapshot").sum())
    source_note = (
        f'<p class="muted">Forecast source: {n_snap_evt} event-days from real bot snapshots '
        f'(NWS + Weather Underground at decision time). Markets without snapshots are excluded.</p>'
    )
    html = source_note + fig_to_html(fig1)
    html += "<h3>Overall accuracy (°F)</h3>" + table_html(acc_all)
    html += fig_to_html(fig2)
    html += fig_to_html(fig3)

    # Dynamic commentary
    takeaways = []
    if not acc_all.empty:
        ens = acc_all[acc_all["source"] == "ensemble"].iloc[0] if (acc_all["source"] == "ensemble").any() else None
        if ens is not None:
            bias_dir = "cold (under-predicts)" if ens["bias"] < -0.2 else ("hot (over-predicts)" if ens["bias"] > 0.2 else "essentially unbiased")
            takeaways.append(
                f"Bot's ensemble (NWS + WU) forecast MAE is <b>{ens['mae']:.2f}°F</b> on "
                f"<b>n={ens['n']}</b> event-days with snapshots. The ensemble runs "
                f"<b>{bias_dir}</b> (bias {ens['bias']:+.2f}°F)."
            )

    if not acc_city.empty:
        ens_city = acc_city[acc_city["source"] == "ensemble"].copy()
        if len(ens_city) >= 3:
            worst = ens_city.nlargest(2, "mae")
            best = ens_city.nsmallest(2, "mae")
            worst_str = ", ".join(f"<b>{r['city_abv']}</b> ({r['mae']:.1f}°F)" for _, r in worst.iterrows())
            best_str = ", ".join(f"<b>{r['city_abv']}</b> ({r['mae']:.1f}°F)" for _, r in best.iterrows())
            takeaways.append(f"Hardest to forecast (largest MAE): {worst_str}. Easiest: {best_str}.")

            biased = ens_city.reindex(ens_city["bias"].abs().sort_values(ascending=False).index).head(2)
            if len(biased) and biased["bias"].abs().max() > 1.0:
                bias_notes = []
                for _, r in biased.iterrows():
                    d = "too cold" if r["bias"] < 0 else "too hot"
                    bias_notes.append(f"<b>{r['city_abv']}</b> ({r['bias']:+.1f}°F, forecasts {d})")
                takeaways.append(f"Cities with material systematic bias: {', '.join(bias_notes)}. Worth a regime-specific correction.")

    if n_snap_evt < 50:
        takeaways.append(
            f"⚠ Only <b>{n_snap_evt}</b> event-days have real bot snapshots — this section is "
            f"thin until the post-Apr 18 snapshot logging fix accumulates more data."
        )

    return exec_summary(summary) + commentary(takeaways) + html


def build_calibration(resolved: pd.DataFrame) -> str:
    summary = (
        '<span class="tl">What this section measures</span>'
        'When the model says "70% chance YES," does YES actually happen 70% of the time? '
        '<b>Brier score</b> is the report card (lower = better; 0.25 = coin flip; '
        '0.0 = perfect). '
        'The <b>reliability diagram</b> plots predicted probability vs. actual outcome rate — '
        'points on the dashed diagonal mean the model is well-calibrated. '
        'Below the line = overconfident; above = underconfident. '
        'We also compare our model&rsquo;s Brier to what the market itself was implying, '
        'to see if we have genuine edge or we&rsquo;re just agreeing with the crowd.'
    )
    if len(resolved) == 0:
        return exec_summary(summary) + '<p class="muted">No resolved-with-snapshot rows.</p>'
    df = resolved.dropna(subset=["yes_probability", "outcome_yes"]).copy()
    if len(df) < 5:
        return exec_summary(summary) + (
            f'<p class="muted">Insufficient resolved markets for calibration '
            f'(need ≥5, have {len(df)}).</p>'
        )

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

    # Dynamic commentary
    takeaways = []
    grade = "excellent" if b_model < 0.10 else ("strong" if b_model < 0.18 else ("fair" if b_model < 0.22 else "poor"))
    takeaways.append(
        f"Model Brier score: <b>{b_model:.3f}</b> ({grade}). Baseline is 0.25 (coin flip); "
        f"perfect is 0.000. Log loss: {ll_model:.3f}. "
        f"Sample size: <b>n={decomp['n']}</b> — {'thin' if decomp['n'] < 50 else 'reasonable'}."
    )
    if not np.isnan(b_mkt):
        diff = b_mkt - b_model
        if diff > 0.01:
            takeaways.append(
                f"Model beats market-implied probability: <b>{b_model:.3f} vs {b_mkt:.3f}</b> "
                f"({diff:+.3f}). Our forecast is more accurate than the market consensus — "
                f"that's the edge we're trading on."
            )
        elif diff < -0.01:
            takeaways.append(
                f"⚠ Market beats the model: {b_model:.3f} vs {b_mkt:.3f} ({diff:+.3f}). "
                f"The bot's probability estimates are less accurate than what the market already "
                f"prices in — edge is unclear."
            )
        else:
            takeaways.append(
                f"Model roughly tied with market: {b_model:.3f} vs {b_mkt:.3f}. "
                f"Edge is marginal, mostly execution-driven."
            )
    # Calibration direction
    if decomp['reliability'] > 0.02:
        # Check if model is systematically over- or under-confident
        with_pred = rel.dropna(subset=["mean_predicted", "actual_yes_rate"])
        if len(with_pred):
            overconf = (with_pred["mean_predicted"] > with_pred["actual_yes_rate"]).mean()
            if overconf > 0.6:
                takeaways.append(
                    f"<b>Model is overconfident</b>: reliability {decomp['reliability']:.3f} "
                    f"and predicted probabilities run higher than actual outcomes. Likely tail "
                    f"mispricing — markets in the 0.7–0.9 predicted range resolve YES less often "
                    f"than the model thinks."
                )
            elif overconf < 0.4:
                takeaways.append(
                    f"<b>Model is underconfident</b>: actual outcomes more extreme than predictions. "
                    f"Could be leaving edge on the table by sizing too conservatively."
                )
    if decomp['n'] < 50:
        takeaways.append(
            f"⚠ With only {decomp['n']} markets, these numbers have wide confidence intervals. "
            "Give it 2 more weeks of snapshot data before drawing conclusions."
        )

    return (exec_summary(summary) + commentary(takeaways) + metrics_html + fig_to_html(fig)
            + "<h3>Bin breakdown</h3>" + table_html(rel))


def build_spread_vs_pnl(resolved: pd.DataFrame) -> str:
    summary = (
        '<span class="tl">What this section measures</span>'
        'When our weather sources disagree (high spread), is the bot <b>more</b> or '
        '<b>less</b> profitable? '
        'If P&L grows with spread, the bot is <i>exploiting</i> market uncertainty — '
        'when others are unsure, we have edge. '
        'If P&L shrinks with spread, the bot is <i>hurt by</i> uncertainty — '
        'we\'re taking positions on days we shouldn\'t. '
        'Buckets split by forecast standard deviation (disagreement between sources) '
        'and range (max − min).'
    )
    if len(resolved) == 0 or resolved["pnl"].isna().all():
        return exec_summary(summary) + '<p class="muted">No data.</p>'

    # Will prepend commentary once we've computed the takeaways
    html_parts = []
    takeaway_lines = []
    for col, label in [("forecast_std", "forecast_std"),
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

        # Analyze trend across bins
        if len(binned) >= 3:
            low_bin = binned.iloc[0]
            high_bin = binned.iloc[-1]
            trend = high_bin["mean_pnl"] - low_bin["mean_pnl"]
            direction = "grows with" if trend > 0.5 else ("shrinks with" if trend < -0.5 else "flat across")
            narrative = ""
            if trend > 1.0:
                narrative = "The bot <b>earns more</b> when sources disagree — exploiting uncertainty is working."
            elif trend < -1.0:
                narrative = "The bot <b>loses money</b> in high-spread conditions — model is mispricing when uncertainty is high. Candidate for a spread-based size reduction."
            else:
                narrative = "Spread doesn't cleanly correlate with P&L — model handles high and low uncertainty similarly."
            takeaway_lines.append(
                f"<b>{label}</b>: mean P&L {direction} spread (${low_bin['mean_pnl']:+.1f} in lowest "
                f"quartile → ${high_bin['mean_pnl']:+.1f} in highest, n={int(low_bin['n'])}+{int(high_bin['n'])}). "
                f"{narrative}"
            )

    charts_html = "".join(html_parts)
    if not charts_html:
        return exec_summary(summary) + '<p class="muted">No usable spread columns.</p>'
    header = exec_summary(summary)
    if takeaway_lines:
        header += commentary(takeaway_lines)
    return header + charts_html


def build_edge_capture(fills: pd.DataFrame) -> str:
    summary = (
        '<span class="tl">What this section measures</span>'
        'For every single trade, "edge" is the gap between what the model thought was '
        'fair and what we actually paid. A +10% edge means the model thought we were '
        'getting a 10 percentage point discount relative to fair odds. '
        'The question: <b>do higher-edge trades actually pay better?</b> '
        'If the scatter trends up-right and the quintile table shows higher P&L in '
        'high-edge buckets, the model&rsquo;s edge signal is <i>real</i>. If not, the '
        'edge is mostly noise.'
    )
    df = fills.dropna(subset=["model_edge_at_fill", "realized_pnl_per_fill"]).copy()
    if len(df) == 0:
        return exec_summary(summary) + (
            '<p class="muted">No fills have snapshot context (limited snapshot coverage).</p>'
        )
    fig = px.scatter(df, x="model_edge_at_fill", y="realized_pnl_per_fill",
                     color="city_abv", hover_data=["market_ticker", "price_paid_dollars"],
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

    takeaways = []
    if len(edge_tbl) >= 3:
        lo = edge_tbl.iloc[0]
        hi = edge_tbl.iloc[-1]
        trend = hi["mean_pnl"] - lo["mean_pnl"]
        if trend > 0.3:
            verdict = "<b>The edge signal is real</b> — higher-edge fills are materially more profitable."
        elif trend < -0.3:
            verdict = "⚠ <b>Edge signal is inverted</b> — fills the model thought were best edge actually lose more. Suggests the model's fair-price calculation is biased in the direction it thinks is profitable."
        else:
            verdict = "Edge signal is weak — no clear relationship between perceived edge and realized P&L."
        takeaways.append(
            f"Mean P&L per fill: lowest-edge quintile <b>${lo['mean_pnl']:+.2f}</b>, "
            f"highest-edge <b>${hi['mean_pnl']:+.2f}</b>. "
            + verdict
        )
        best_bin = edge_tbl.loc[edge_tbl["mean_pnl"].idxmax()]
        takeaways.append(
            f"Best edge bin: {best_bin['edge_bin']} → "
            f"<b>${best_bin['mean_pnl']:+.2f}/fill</b> avg P&L, "
            f"<b>{best_bin['win_rate']:.0%}</b> win rate on {int(best_bin['n'])} fills."
        )
    takeaways.append(
        f"⚠ Only <b>{len(df)} fills</b> have snapshot context (the rest fall in the Mar 11 → "
        "Apr 18 logging gap). This analysis is preliminary until snapshot data accumulates."
    )

    return (exec_summary(summary) + commentary(takeaways) + fig_to_html(fig)
            + "<h3>P&L by edge quintile</h3>" + table_html(edge_tbl))


def build_execution(orders: pd.DataFrame, fills: pd.DataFrame) -> str:
    summary = (
        '<span class="tl">What this section measures</span>'
        'Even a great model can be ruined by bad execution. This section grades <b>how '
        'the bot places and fills orders</b>, not its forecasting. '
        'The bot posts limit orders and waits (never pays the spread), so <b>fill rate '
        'is low by design</b> (~3%) — the tradeoff is better prices. '
        'Key checks: Are we getting filled disproportionately on bad prices? Are fees '
        'eating returns? Are maker fills (we posted) more profitable than taker fills '
        '(we chased)?'
    )
    by_city = M.fill_rate_by(orders, "city_abv").head(25)
    fig1 = px.bar(by_city, x="city_abv", y="fill_rate_contracts",
                  title="Fill rate (contracts) by city")
    fig1.update_layout(height=400)

    # Fill rate by price bucket (dollars)
    o = orders.copy()
    o["price_bucket"] = pd.cut(o["ordered_no_price_dollars"],
                               bins=[0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    pb = (o.groupby("price_bucket", observed=True)
           .agg(n_orders=("client_order_id", "size"),
                n_filled=("any_fill", "sum"),
                total_ordered=("ordered_contracts", "sum"),
                total_filled=("filled_contracts", "sum"))
           .reset_index())
    pb["fill_rate"] = pb["total_filled"] / pb["total_ordered"].where(pb["total_ordered"] > 0, np.nan)
    pb["price_bucket"] = pb["price_bucket"].astype(str)
    fig2 = px.bar(pb, x="price_bucket", y="fill_rate",
                  title="Fill rate by limit NO-price bucket (dollars)")
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
    # Dynamic commentary
    takeaways = []
    overall_fill_rate_orders = orders["any_fill"].mean()
    overall_fill_rate_contracts = orders["filled_contracts"].sum() / orders["ordered_contracts"].sum() if orders["ordered_contracts"].sum() else 0
    takeaways.append(
        f"Fill rate: <b>{overall_fill_rate_orders:.1%} of orders</b> and "
        f"<b>{overall_fill_rate_contracts:.1%} of contracts</b> across "
        f"{len(orders):,} placed orders. Low is expected for a post-only bot."
    )
    if total_fees > 0 and total_gross > 0:
        fee_pct = total_fees / total_gross * 100
        fee_verdict = "negligible" if fee_pct < 2 else ("moderate" if fee_pct < 10 else "<b>material</b>")
        takeaways.append(
            f"Fees: <b>${total_fees:,.2f}</b> ({fee_pct:.1f}% of gross) — {fee_verdict}. "
            f"Net P&L ${total_net:,.2f} vs gross ${total_gross:,.2f}."
        )
    if len(taker) > 0 and len(maker) > 0:
        taker_avg = taker["realized_pnl_per_fill"].mean()
        maker_avg = maker["realized_pnl_per_fill"].mean()
        takeaways.append(
            f"Maker vs taker: <b>{len(maker):,} maker fills</b> (avg ${maker_avg:+.2f}/fill) "
            f"vs <b>{len(taker):,} taker fills</b> (avg ${taker_avg:+.2f}/fill). "
            f"{'Makers more profitable — post-only strategy paying off.' if maker_avg > taker_avg else 'Takers more profitable — but bot is post-only by design; check why this category exists.'}"
        )
    elif len(taker) == 0:
        takeaways.append(
            "All fills are <b>maker</b> (post-only). No takers — as designed."
        )
    # Look at city-level fill rate patterns
    if len(by_city) >= 3:
        top = by_city.nlargest(2, "fill_rate_contracts")
        bot = by_city.nsmallest(2, "fill_rate_contracts")
        takeaways.append(
            f"Highest fill rate cities: "
            f"{', '.join(f'<b>{r.city_abv}</b> ({r.fill_rate_contracts:.1%})' for _, r in top.iterrows())}. "
            f"Lowest: "
            f"{', '.join(f'<b>{r.city_abv}</b> ({r.fill_rate_contracts:.1%})' for _, r in bot.iterrows())}. "
            "Low fill rate ≠ bad — often means our limit prices are conservative."
        )

    return (exec_summary(summary) + commentary(takeaways)
            + fig_to_html(fig1) + fig_to_html(fig2)
            + f'<div class="cards">{exec_cards}</div>'
            + "<h3>By city (top 25)</h3>" + table_html(by_city))


def build_pnl_attribution(settle: pd.DataFrame) -> str:
    summary = (
        '<span class="tl">What this section measures</span>'
        'The money story: where P&L came from, and where it didn&rsquo;t. '
        'The <b>cumulative P&L curve</b> shows whether we made money steadily or in '
        'big jumps. <b>Drawdown</b> shows the worst peak-to-trough pain along the way. '
        'The city breakdown tells us which markets are profitable and which to cut. '
        'The weekday pattern can reveal regime effects (e.g., forecasts less accurate '
        'on weekends). Red cities in the bar chart are losing money despite our bets — '
        'candidates for either size reduction or blocking.'
    )
    # settle is now KXHIGH_settlements_clean — pnl_dollars, settled_ts, city_abv
    df = settle.copy().rename(columns={"pnl_dollars": "pnl", "total_cost_dollars": "total_cost"})
    df["settled_ts"] = pd.to_datetime(df["settled_ts"], utc=True)
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
    df["city_abv"] = df["city_abv"].fillna("?")
    by_city = df.groupby("city_abv").agg(
        n=("pnl", "size"), total_pnl=("pnl", "sum"),
        mean_pnl=("pnl", "mean"),
        win_rate=("pnl", lambda x: (x > 0).mean()),
        total_cost=("total_cost", "sum"),
    ).reset_index()
    by_city["roi"] = by_city["total_pnl"] / by_city["total_cost"].where(by_city["total_cost"] > 0, np.nan)
    by_city = by_city.sort_values("total_pnl", ascending=False)
    fig2 = px.bar(by_city, x="city_abv", y="total_pnl",
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

    # Dynamic commentary
    takeaways = []
    total = df["pnl"].sum()
    total_cost = df["total_cost"].sum()
    overall_roi = total / total_cost if total_cost else 0
    max_dd = df["drawdown"].min()
    max_dd_date = df.loc[df["drawdown"].idxmin(), "settled_ts"].date() if len(df) else None
    takeaways.append(
        f"Total P&L: <b>${total:,.2f}</b> on <b>${total_cost:,.0f}</b> of cost basis → "
        f"<b>{overall_roi:.1%} ROI</b>. Worst drawdown: <b>${abs(max_dd):,.0f}</b> "
        f"(trough around {max_dd_date})."
    )
    # Top/bottom cities
    if len(by_city) >= 2:
        winners = by_city.head(3)
        losers = by_city.tail(3)
        w_str = ", ".join(f"<b>{r.city_abv}</b> (${r.total_pnl:+,.0f}, ROI {r.roi:.0%})" for _, r in winners.iterrows())
        l_str = ", ".join(f"<b>{r.city_abv}</b> (${r.total_pnl:+,.0f}, ROI {r.roi:.0%})" for _, r in losers.iterrows())
        takeaways.append(f"Best cities: {w_str}.")
        takeaways.append(f"Worst cities: {l_str}. Candidates for size reduction or blocking.")
        # Cities losing money
        losing_cities = by_city[by_city["total_pnl"] < 0]
        if len(losing_cities) > 0:
            losing_total = losing_cities["total_pnl"].sum()
            takeaways.append(
                f"<b>{len(losing_cities)} of {len(by_city)}</b> cities lost money "
                f"(combined ${losing_total:,.0f}). Cutting these would boost net P&L to "
                f"<b>${total - losing_total:,.0f}</b>."
            )
    # Weekday patterns
    wd_nonzero = wd.dropna(subset=["total_pnl"]) if "total_pnl" in wd.columns else pd.DataFrame()
    if len(wd_nonzero) >= 3:
        best_wd = wd_nonzero.loc[wd_nonzero["total_pnl"].idxmax()]
        worst_wd = wd_nonzero.loc[wd_nonzero["total_pnl"].idxmin()]
        if best_wd["total_pnl"] - worst_wd["total_pnl"] > 30:
            takeaways.append(
                f"Weekday pattern: <b>{best_wd['weekday']}</b> is the best day "
                f"(${best_wd['total_pnl']:+,.0f} on {int(best_wd['n'])} markets), "
                f"<b>{worst_wd['weekday']}</b> the worst (${worst_wd['total_pnl']:+,.0f}). "
                f"Could reflect forecast accuracy variation (weekends use older NWS packages)."
            )
    # Daily volatility
    if len(daily) >= 5:
        daily_std = daily["pnl"].std()
        pos_days = (daily["pnl"] > 0).sum()
        takeaways.append(
            f"Daily P&L volatility: std <b>${daily_std:,.0f}/day</b>. "
            f"<b>{pos_days} of {len(daily)}</b> settlement-days were net positive "
            f"({pos_days/len(daily):.0%})."
        )

    return (exec_summary(summary) + commentary(takeaways)
            + fig_to_html(fig) + fig_to_html(fig2)
            + fig_to_html(fig3) + fig_to_html(fig4)
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
.exec { background: #f0f4f8; border-left: 4px solid #4a90b8; padding: 14px 18px;
        margin: 0 0 20px 0; border-radius: 4px; font-size: 14px; line-height: 1.55;
        color: #1a2a3a; }
.exec b { color: #0d2840; }
.exec .tl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
           color: #4a90b8; font-weight: 600; margin-bottom: 6px; display: block; }
.cmt { background: #f9f7f1; border-left: 4px solid #c79a5c; padding: 14px 18px;
       margin: 24px 0 0 0; border-radius: 4px; font-size: 14px; line-height: 1.6;
       color: #3a2f1a; }
.cmt .ctl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.6px;
            color: #9a6b2d; font-weight: 600; margin-bottom: 6px; }
.cmt .chd { font-weight: 600; font-size: 15px; color: #2a2010; margin-bottom: 8px; }
.cmt ul { margin: 0; padding-left: 22px; }
.cmt li { margin-bottom: 4px; }
.cmt b { color: #5a3f10; }
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


def build_filter_sensitivity(resolved: pd.DataFrame) -> str:
    """Two structural filter analyses: bucket-distance and source-agreement.
    Tests whether removing certain markets *would have* improved P&L.

    Filter 1 — Bucket distance: skip markets where |bucket center − forecast μ|
               is small (i.e., centered buckets where model is most confident).
    Filter 2 — Source agreement: skip markets where |NWS − WU| is small (i.e.,
               sources agree, model is confident this is the right call).

    Both target the same structural failure mode (high-P(yes) buckets where
    fair_NO < hi_no_config → bot overpays).
    """
    summary = (
        '<span class="tl">What this section measures</span>'
        'Two filter ideas tested as counterfactuals: would P&L improve if we '
        '<b>skipped</b> certain markets at the time of trade? '
        '<b>Distance</b> filter targets bucket centers very close to the forecast μ '
        '— markets where the model is confident, fair NO is low, and the bot tends '
        'to overpay relative to fair (the 4/23 loss pattern). '
        '<b>Source agreement</b> filter targets markets where NWS and WU agree '
        'tightly — same signal from a different angle. '
        'For each threshold, we compute the P&L of the markets that <i>survive</i> '
        'the filter (= markets we would have traded). Higher than control = filter helps; '
        'lower = filter cuts profitable markets too.'
    )
    if resolved.empty or "pnl" not in resolved.columns:
        return exec_summary(summary) + '<p class="muted">No data.</p>'

    df = resolved.copy()
    # Restrict to settled "between" markets where μ + bucket midpoint exist
    df = df[(df["market_kind"] == "between") & df["pnl"].notna()
            & df["bucket_midpoint"].notna() & df["forecast_avg"].notna()].copy()
    if df.empty:
        return exec_summary(summary) + '<p class="muted">No settled B markets with required fields.</p>'

    df["distance_from_mu"] = (df["bucket_midpoint"] - df["forecast_avg"]).abs()
    df["nws_wu_diff"] = (df["nws"] - df["weather_underground"]).abs()
    control_pnl = df["pnl"].sum()
    n_total = len(df)

    # ---- Filter 1: distance ----
    dist_rows = []
    for thresh in [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]:
        keep = df["distance_from_mu"] >= thresh
        kept_pnl = float(df.loc[keep, "pnl"].sum())
        dist_rows.append({
            "min_distance_F": thresh,
            "markets_kept": int(keep.sum()),
            "kept_share": f"{100*keep.mean():.0f}%",
            "kept_pnl": round(kept_pnl, 2),
            "delta_vs_control": round(kept_pnl - control_pnl, 2),
        })
    dist_df = pd.DataFrame(dist_rows)
    best_dist = dist_df.loc[dist_df["delta_vs_control"].idxmax()]

    fig_d = go.Figure()
    fig_d.add_trace(go.Bar(
        x=dist_df["min_distance_F"], y=dist_df["delta_vs_control"],
        marker_color=["seagreen" if v > 0 else "indianred" for v in dist_df["delta_vs_control"]],
        text=dist_df["markets_kept"], texttemplate="n=%{text}",
        textposition="outside",
    ))
    fig_d.update_layout(
        title="Distance filter — Δ P&L vs control by minimum |bucket center − μ| threshold",
        xaxis_title="Minimum distance threshold (°F)",
        yaxis_title="Δ P&L vs no filter ($)",
        height=400, showlegend=False,
    )
    fig_d.add_hline(y=0, line_dash="dash", line_color="gray")

    # Distance distribution (informational)
    fig_d_hist = go.Figure()
    fig_d_hist.add_trace(go.Histogram(x=df["distance_from_mu"], nbinsx=30, marker_color="steelblue"))
    fig_d_hist.update_layout(
        title=f"Distribution of |bucket center − μ| across {n_total} settled markets",
        xaxis_title="|bucket center − μ| (°F)", yaxis_title="markets", height=280,
    )

    # ---- Filter 2: source agreement (only on markets where both NWS+WU present) ----
    sa_df = df[df["nws"].notna() & df["weather_underground"].notna()].copy()
    agreement_html = ""
    if len(sa_df) > 0:
        sa_control = sa_df["pnl"].sum()
        sa_rows = []
        for thresh in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
            keep = sa_df["nws_wu_diff"] <= thresh
            kept_pnl = float(sa_df.loc[keep, "pnl"].sum())
            sa_rows.append({
                "max_NWS_WU_diff_F": thresh,
                "markets_kept": int(keep.sum()),
                "kept_share": f"{100*keep.mean():.0f}%",
                "kept_pnl": round(kept_pnl, 2),
                "delta_vs_control": round(kept_pnl - sa_control, 2),
            })
        sa_table = pd.DataFrame(sa_rows)
        best_sa = sa_table.loc[sa_table["delta_vs_control"].idxmax()]

        fig_a = go.Figure()
        fig_a.add_trace(go.Bar(
            x=sa_table["max_NWS_WU_diff_F"], y=sa_table["delta_vs_control"],
            marker_color=["seagreen" if v > 0 else "indianred" for v in sa_table["delta_vs_control"]],
            text=sa_table["markets_kept"], texttemplate="n=%{text}",
            textposition="outside",
        ))
        fig_a.update_layout(
            title="Source-agreement filter — Δ P&L vs control by maximum |NWS − WU| threshold",
            xaxis_title="Maximum |NWS − WU| threshold (°F)",
            yaxis_title="Δ P&L vs no filter ($)",
            height=400, showlegend=False,
        )
        fig_a.add_hline(y=0, line_dash="dash", line_color="gray")
        agreement_html = (
            f'<h3>Source-agreement filter</h3>'
            f'<p class="muted">Sample: {len(sa_df)} settled markets where both NWS and WU '
            f'were captured. Subset control P&L: ${sa_control:.2f}.</p>'
            + fig_to_html(fig_a) + table_html(sa_table)
        )

    # ---- Per-city impact at distance >= 1°F ----
    keep_1f = df["distance_from_mu"] >= 1.0
    df["_pnl_filt"] = np.where(keep_1f, df["pnl"], 0.0)
    per_city = df.groupby("city").agg(
        n=("market_ticker", "count"),
        ctrl_pnl=("pnl", "sum"),
        filt_pnl=("_pnl_filt", "sum"),
    ).round(2)
    per_city["delta"] = per_city["filt_pnl"] - per_city["ctrl_pnl"]
    per_city = per_city.sort_values("delta", ascending=False).reset_index()
    fig_city = go.Figure()
    fig_city.add_trace(go.Bar(
        x=per_city["city"], y=per_city["delta"],
        marker_color=["seagreen" if v > 0 else "indianred" for v in per_city["delta"]],
    ))
    fig_city.update_layout(
        title="Distance ≥ 1°F filter — per-city Δ vs control",
        xaxis_title="city", yaxis_title="Δ P&L ($)", height=420, showlegend=False,
    )
    fig_city.add_hline(y=0, line_dash="dash", line_color="gray")

    # ---- Takeaways ----
    bullets = []
    bullets.append(
        f"<b>Distance filter best:</b> threshold = "
        f"<b>{float(best_dist['min_distance_F'])}°F</b> "
        f"(keeps {best_dist['markets_kept']} of {n_total} markets, "
        f"Δ = <b>${float(best_dist['delta_vs_control']):+.2f}</b> vs control ${control_pnl:.2f})."
    )
    if len(sa_df) > 0:
        bullets.append(
            f"<b>Agreement filter best:</b> threshold = "
            f"<b>≤{float(best_sa['max_NWS_WU_diff_F'])}°F</b> "
            f"(keeps {best_sa['markets_kept']} of {len(sa_df)} markets, "
            f"Δ = <b>${float(best_sa['delta_vs_control']):+.2f}</b> vs subset control ${sa_df['pnl'].sum():.2f})."
        )
    bullets.append(
        "<b>Per-city pattern:</b> distance filter helps the loss-prone cities "
        "(Miami, Phoenix, Houston, NOLA, Philadelphia) and hurts the win-prone ones "
        "(Austin, LA, Seattle). Same structural trade-off as the A/B treatment cap."
    )
    bullets.append(
        "<b>Caution:</b> these are in-sample optima on a still-small dataset. "
        "Bootstrap CIs (computed in <code>analysis/kxhigh/python/backtest_filters.py</code>) "
        "are wide enough that none of these filter levels reach statistical "
        "significance yet. Re-evaluate after 4-6 weeks of accumulated fills."
    )

    parts = [
        exec_summary(summary),
        commentary(bullets),
        '<h3>Distance filter — sensitivity to threshold</h3>',
        fig_to_html(fig_d),
        table_html(dist_df),
        fig_to_html(fig_d_hist),
        agreement_html,
        '<h3>Per-city impact (distance ≥ 1°F filter)</h3>',
        fig_to_html(fig_city),
        table_html(per_city),
    ]
    return "".join(parts)


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
        ("filters", "7 · Filter sensitivity (distance + source agreement)",
         build_filter_sensitivity(data["resolved"])),
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
