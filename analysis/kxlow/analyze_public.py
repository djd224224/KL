# -*- coding: utf-8 -*-
"""KXLOW low-temp bot performance analysis from PUBLIC data.

Reconstructs the bot's activity from the anonymous public trade tape using
its known execution signature (post-only NO-side maker, 8-rung 2c ladder
capped at 50c, 2-4 contracts/rung, orders only live from the evening run
until local 02:59 of the target day), marks estimated fills against actual
settlements, and validates the forecast side against NWS CLI actual lows.

Outputs aggregate CSVs into analysis/kxlow/output/ and prints a summary.
Exact account P&L still requires the private settlements API / BQ fills —
this is the best public-data estimate, clearly labeled as such.
"""

import gzip
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "output")
os.makedirs(OUT, exist_ok=True)

GO_LIVE = date(2026, 7, 22)          # first live run (evening of Jul 22)
EST_START = date(2026, 7, 23)        # first event the bot could have quoted
EST_END = date(2026, 8, 24)          # last fully settled event at pull time
SIZE_RAISE = date(2026, 7, 25)       # targets from here quoted 2/rung day-of, 4/rung evening
TODAY = date(2026, 8, 25)

CITY_TZ = {
    "New York City": "America/New_York", "Philadelphia": "America/New_York",
    "Miami": "America/New_York", "Atlanta": "America/New_York",
    "Washington DC": "America/New_York", "Boston": "America/New_York",
    "Chicago": "America/Chicago", "Austin": "America/Chicago",
    "Houston": "America/Chicago", "Dallas": "America/Chicago",
    "Oklahoma City": "America/Chicago", "San Antonio": "America/Chicago",
    "Minneapolis": "America/Chicago", "New Orleans": "America/Chicago",
    "Denver": "America/Denver", "Phoenix": "America/Phoenix",
    "Las Vegas": "America/Los_Angeles", "Seattle": "America/Los_Angeles",
    "San Francisco": "America/Los_Angeles", "Los Angeles": "America/Los_Angeles",
}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def event_date(event_ticker):
    try:
        tail = event_ticker.rsplit("-", 1)[1]
        return date(2000 + int(tail[:2]), MONTHS[tail[2:5]], int(tail[5:7]))
    except Exception:
        return None


def load_markets():
    raw_path = os.path.join(DATA, "kxlow_markets_raw.json.gz")
    if os.path.exists(raw_path):
        with gzip.open(raw_path, "rt") as f:
            raw = json.load(f)
        m = pd.json_normalize(raw)
        m["city"] = m["_city"]
    else:
        m = pd.read_csv(os.path.join(DATA, "kxlow_markets.csv.gz"))
    m["date"] = m["event_ticker"].map(event_date)
    m = m[m["date"].notna()].copy()
    # Band in settled integer °F terms:
    #   between floor..cap inclusive; less: <= cap-1; greater: >= floor+1
    m["band_lo"] = np.where(m.strike_type.eq("between"), m.floor_strike,
                   np.where(m.strike_type.eq("greater"), m.floor_strike + 1, -999))
    m["band_hi"] = np.where(m.strike_type.eq("between"), m.cap_strike,
                   np.where(m.strike_type.eq("less"), m.cap_strike - 1, 999))
    return m


def load_cli():
    c = pd.read_csv(os.path.join(DATA, "cli_lows.csv.gz"))
    c["date"] = pd.to_datetime(c["date"]).dt.date
    c["low"] = pd.to_numeric(c["low"], errors="coerce")
    return c[["city", "date", "low", "low_time"]].dropna(subset=["low"])


def load_forecast():
    fp = pd.read_csv(os.path.join(DATA, "forecast_proxy.csv.gz"))
    fp["dt"] = pd.to_datetime(fp.time_local)
    fp["date"] = fp.dt.dt.date
    day_min = (fp.groupby(["city", "model", "date"])[["t_prev1", "t_prev2"]]
                 .min().reset_index()
                 .rename(columns={"t_prev1": "fc_min_prev1", "t_prev2": "fc_min_prev2"}))
    return day_min


def load_trades(markets):
    t = pd.read_csv(os.path.join(DATA, "kxlow_trades.csv.gz"))
    if t.empty:
        return t
    t["created"] = pd.to_datetime(t.created_time, utc=True, format="mixed")
    mk = markets[["ticker", "date", "strike_type", "band_lo", "band_hi",
                  "result", "status"]]
    t = t.merge(mk, on="ticker", how="left")

    # City-local trade time and the bot's live-order window for that event:
    # evening run ~19:17 CT the night before -> orders rest to local 23:59;
    # day-of runs -> orders rest to local 02:59 of the target day.
    local = []
    for city, gd in t.groupby("city"):
        tz = ZoneInfo(CITY_TZ[city])
        local.append(gd.created.dt.tz_convert(tz).dt.tz_localize(None))
    t["local_dt"] = pd.concat(local).sort_index()
    ev_midnight = pd.to_datetime(t["date"].astype(str))
    t["hrs_from_midnight"] = (t.local_dt - ev_midnight).dt.total_seconds() / 3600.0
    # Window: from 18:00 local the evening before (first possible evening-run
    # quote is ~19:17 CT; 18:00 local is safely before it in every tz) to
    # 03:00 local on the target day (all bot orders expired by 02:59).
    t["in_bot_window"] = (t.hrs_from_midnight >= -6.0) & (t.hrs_from_midnight <= 3.0)

    t["maker_no"] = t.taker_side.eq("yes")   # taker bought YES -> resting NO bid filled
    rung_max = np.where(pd.to_datetime(t["date"].astype(str)).dt.date < SIZE_RAISE, 2, 4)
    t["bot_like"] = (t.maker_no & t.in_bot_window
                     & t.strike_type.eq("between")
                     & (t.no_price <= 50)
                     & (t["count"] <= rung_max))

    # Maker-NO P&L (gross; the bot's fills are post-only maker, no taker fee)
    settled = t.result.isin(["yes", "no"])
    t["maker_no_pnl"] = np.where(
        ~settled, np.nan,
        np.where(t.result.eq("no"),
                 t["count"] * (100 - t.no_price) / 100.0,
                 -t["count"] * t.no_price / 100.0))
    t["maker_no_cost"] = t["count"] * t.no_price / 100.0
    return t


def main():
    m = load_markets()
    cli = load_cli()
    fc = load_forecast()

    era = m[(m.date >= GO_LIVE) & (m.date <= TODAY)].copy()
    settled = era[era.status.eq("finalized") & era.result.isin(["yes", "no"])].copy()

    # ---- settlement sanity: winning band vs CLI actual low
    s = settled.merge(cli, on=["city", "date"], how="left")
    s["pred_yes"] = (s.low >= s.band_lo) & (s.low <= s.band_hi)
    have = s.dropna(subset=["low"])
    mism = have[have.pred_yes != have.result.eq("yes")]
    print(f"settlement check: {len(have)} settled markets with CLI lows, "
          f"{len(mism)} mismatches ({len(mism)/max(len(have),1):.2%})")
    if len(mism):
        mism.groupby(["city"]).size().to_csv(os.path.join(OUT, "settle_mismatch_by_city.csv"))
        mism[["ticker", "date", "strike_type", "band_lo", "band_hi", "low", "result"]] \
            .to_csv(os.path.join(OUT, "settle_mismatches.csv"), index=False)

    # ---- forecast accuracy vs CLI low (evening-before vantage)
    f = fc.merge(cli, on=["city", "date"], how="inner")
    f = f[(f.date >= GO_LIVE) & (f.date <= TODAY)]
    f["err1"] = f.fc_min_prev1 - f.low
    fstats = (f.groupby(["city", "model"])["err1"]
                .agg(n="count", bias="mean",
                     mae=lambda x: x.abs().mean(),
                     sd="std").round(2).reset_index())
    fstats.to_csv(os.path.join(OUT, "forecast_err_by_city.csv"), index=False)

    blend = (f.groupby(["city", "date"])
               .agg(fc=("fc_min_prev1", "mean"), low=("low", "first")).reset_index())
    blend["err"] = blend.fc - blend.low
    blend.to_csv(os.path.join(OUT, "forecast_blend_daily.csv"), index=False)

    # ---- trade tape
    t = load_trades(m)
    if t.empty:
        print("NO TRADES DATA YET"); return
    t = t[t["date"].notna()].copy()
    t.to_pickle(os.path.join(OUT, "trades_enriched.pkl"))

    # ---- attribution controls (measure the look-alike flow) --------------
    # (a) pre-live events (Jul 16-22): the bot did not exist -> every
    #     signature match is a false positive.  (b) Thursday day-of windows
    #     during the live era: the Kalshi maintenance pause blocks both
    #     day-of triggers, so the bot is provably absent there too.
    t["session"] = np.where(t.hrs_from_midnight < 0, "evening", "day-of")
    t["dow"] = pd.to_datetime(t["date"].astype(str)).dt.dayofweek
    pre = t[(t["date"] >= date(2026, 7, 16)) & (t["date"] <= date(2026, 7, 22))]
    live_all = t[(t["date"] >= EST_START) & (t["date"] <= EST_END)]
    pre_sig = pre[pre.bot_like]
    thu = live_all[live_all.bot_like & (live_all.dow == 3) & live_all.session.eq("day-of")]
    nonthu = live_all[live_all.bot_like & (live_all.dow != 3) & live_all.session.eq("day-of")]
    thu_days = max(live_all[live_all.dow == 3]["date"].nunique(), 1)
    nonthu_days = max(live_all[live_all.dow != 3]["date"].nunique(), 1)
    thu_rate = thu["count"].sum() / thu_days
    nonthu_rate = nonthu["count"].sum() / nonthu_days
    contam_share = min(thu_rate / nonthu_rate, 1.0) if nonthu_rate else 0.0
    bot_share = 1.0 - contam_share
    controls = {
        "pre_live_sig_contracts_per_day": round(pre_sig["count"].sum() / 7, 1),
        "pre_live_sig_roi": round(float(pre_sig.maker_no_pnl.sum() / pre_sig.maker_no_cost.sum()), 3)
                            if pre_sig.maker_no_cost.sum() else None,
        "thu_dayof_contracts_per_day": round(thu_rate, 1),
        "nonthu_dayof_contracts_per_day": round(nonthu_rate, 1),
        "contamination_share": round(contam_share, 3),
        "bot_share": round(bot_share, 3),
    }
    ctrl_cost = float(pre_sig.maker_no_cost.sum() + thu.maker_no_cost.sum())
    ctrl_pnl = float(pre_sig.maker_no_pnl.sum() + thu.maker_no_pnl.sum())
    controls["lookalike_roi_combined"] = round(ctrl_pnl / ctrl_cost, 3) if ctrl_cost else None
    controls["thu_lookalike_roi"] = (round(float(thu.maker_no_pnl.sum() / thu.maker_no_cost.sum()), 3)
                                     if thu.maker_no_cost.sum() else None)

    # per-city look-alike rate (contracts/day): mean of the two era controls
    live_days = (EST_END - EST_START).days + 1
    pre_city = pre_sig.groupby("city")["count"].sum() / 7
    thu_city = thu.groupby("city")["count"].sum() / thu_days
    strict_city = (live_all[live_all.bot_like & live_all.result.isin(["yes", "no"])]
                   .groupby("city")["count"].sum() / live_days)
    ctrl_city = pd.concat([pre_city.rename("pre"), thu_city.rename("thu"),
                           strict_city.rename("strict")], axis=1).fillna(0.0)
    # Thursday control only covers the day-of half of the window; the
    # pre-live control covers both sessions. Use pre-live as the primary
    # per-city look-alike rate, floored by the Thursday day-of rate.
    ctrl_city["lookalike"] = ctrl_city[["pre", "thu"]].max(axis=1)
    ctrl_city["bot_share_city"] = (1 - ctrl_city.lookalike / ctrl_city.strict.replace(0, np.nan)).clip(0, 1).fillna(0)
    ctrl_city = ctrl_city.round(3)
    print(f"\nCONTROLS: pre-live sig {controls['pre_live_sig_contracts_per_day']}/day; "
          f"Thu day-of {thu_rate:.0f}/day vs non-Thu {nonthu_rate:.0f}/day "
          f"-> contamination ~{contam_share:.0%}, bot share ~{bot_share:.0%}")

    t = t[(t["date"] >= EST_START) & (t["date"] <= TODAY)].copy()

    print(f"\ntape: {len(t)} trades, {t['count'].sum():.0f} contracts, "
          f"{t.ticker.nunique()} markets, {t.date.min()} -> {t.date.max()}")
    print("taker_side:", t.taker_side.value_counts().to_dict())
    print("count distribution (maker_no, in window, between, <=50c):")
    tb = t[t.maker_no & t.in_bot_window & t.strike_type.eq("between") & (t.no_price <= 50)]
    print(tb["count"].value_counts().sort_index().head(12).to_string())

    bot_all = t[t.bot_like].copy()
    open_pos = bot_all[~bot_all.result.isin(["yes", "no"])]
    bot = bot_all[bot_all.result.isin(["yes", "no"]) & (bot_all["date"] <= EST_END)].copy()
    print(f"\nbot-like fills (settled, {EST_START}..{EST_END}): {len(bot)} trades, "
          f"{bot['count'].sum():.0f} contracts, cost ${bot.maker_no_cost.sum():,.0f}, "
          f"est P&L ${bot.maker_no_pnl.sum():,.2f}  "
          f"(+ open/unsettled exposure ${open_pos.maker_no_cost.sum():,.0f})")

    # aggregates
    by_day = (bot.groupby("date")
                 .agg(trades=("count", "size"), contracts=("count", "sum"),
                      cost=("maker_no_cost", "sum"), pnl=("maker_no_pnl", "sum"))
                 .reset_index())
    by_day.to_csv(os.path.join(OUT, "bot_est_by_day.csv"), index=False)

    by_city = (bot.groupby("city")
                  .agg(trades=("count", "size"), contracts=("count", "sum"),
                       cost=("maker_no_cost", "sum"), pnl=("maker_no_pnl", "sum"))
                  .reset_index().sort_values("pnl"))
    by_city.to_csv(os.path.join(OUT, "bot_est_by_city.csv"), index=False)

    # per city-day market outcomes for the win-mechanics view
    daymk = (bot.groupby(["city", "date", "ticker", "band_lo", "band_hi", "result"])
                .agg(contracts=("count", "sum"), cost=("maker_no_cost", "sum"),
                     pnl=("maker_no_pnl", "sum"),
                     avg_no=("no_price", "mean")).reset_index())
    daymk = daymk.merge(cli, on=["city", "date"], how="left")
    daymk.to_csv(os.path.join(OUT, "bot_est_fills_by_market.csv"), index=False)

    # whole NO-maker class per city (all sizes/prices, in-window or not)
    klass = (t[t.maker_no & t.strike_type.eq("between")]
             .groupby("city")
             .agg(trades=("count", "size"), contracts=("count", "sum"),
                  cost=("maker_no_cost", "sum"), pnl=("maker_no_pnl", "sum"))
             .reset_index().sort_values("pnl"))
    klass.to_csv(os.path.join(OUT, "maker_no_class_by_city.csv"), index=False)

    # evening vs day-of fills (orders live -6h..0 = evening run rest window,
    # 0..+3h local = day-of window during the post-midnight info reveal)
    bot["session"] = np.where(bot.hrs_from_midnight < 0, "evening", "day-of")
    sess = (bot.groupby("session")
               .agg(trades=("count", "size"), contracts=("count", "sum"),
                    cost=("maker_no_cost", "sum"), pnl=("maker_no_pnl", "sum"))
               .reset_index())
    sess["roi"] = (sess.pnl / sess.cost).round(3)
    sess.to_csv(os.path.join(OUT, "bot_est_by_session.csv"), index=False)

    # price-level edge: at NO price p the market says P(win)=p/100 for the
    # maker; realized share of winners above that = gross edge
    bins = [0, 15, 25, 35, 45, 51]
    bot["pbin"] = pd.cut(bot.no_price, bins, right=False)
    bot["win_wt"] = bot["count"] * bot.result.eq("no")
    pe = (bot.groupby("pbin", observed=True)
             .apply(lambda g: pd.Series({
                 "contracts": g["count"].sum(),
                 "cost": g.maker_no_cost.sum(),
                 "pnl": g.maker_no_pnl.sum(),
                 "avg_no": np.average(g.no_price, weights=g["count"]),
                 "win_share": g.win_wt.sum() / g["count"].sum(),
             }), include_groups=False)
             .reset_index())
    pe["pbin"] = pe.pbin.astype(str)
    pe.to_csv(os.path.join(OUT, "bot_est_price_edge.csv"), index=False)

    # weekly trend
    wk = bot.copy()
    wk["week"] = pd.to_datetime(wk["date"].astype(str)).dt.to_period("W-SUN").astype(str)
    wkly = (wk.groupby("week")
              .agg(contracts=("count", "sum"), cost=("maker_no_cost", "sum"),
                   pnl=("maker_no_pnl", "sum")).reset_index())
    wkly.to_csv(os.path.join(OUT, "bot_est_by_week.csv"), index=False)

    # sensitivity bounds on the attribution filter
    variants = {
        "strict (window+size+<=50c)": t.bot_like,
        "window, any size <=50c": (t.maker_no & t.in_bot_window
                                   & t.strike_type.eq("between") & (t.no_price <= 50)),
        "all maker-NO on buckets": t.maker_no & t.strike_type.eq("between"),
    }
    sens_rows = []
    for name, mask in variants.items():
        g = t[mask]
        sens_rows.append({"variant": name, "trades": len(g),
                          "contracts": g["count"].sum(),
                          "cost": g.maker_no_cost.sum().round(2),
                          "pnl": g.maker_no_pnl.sum().round(2)})
    sens = pd.DataFrame(sens_rows)
    sens.to_csv(os.path.join(OUT, "attribution_sensitivity.csv"), index=False)

    # ---- dashboard payload -------------------------------------------------
    daily = by_day.copy()
    daily["cum"] = daily.pnl.cumsum()
    peak = daily.cum.cummax()
    max_dd = float((daily.cum - peak).min()) if len(daily) else 0.0

    per_mkt = daymk.copy()
    city_tbl = (per_mkt.groupby("city")
                .agg(mkts=("ticker", "nunique"),
                     wins=("result", lambda r: r.eq("no").sum()),
                     contracts=("contracts", "sum"),
                     cost=("cost", "sum"), pnl=("pnl", "sum"),
                     avg_no=("avg_no", "mean")).reset_index())
    city_tbl["win_rate"] = (city_tbl.wins / city_tbl.mkts).round(3)
    city_tbl["roi"] = (city_tbl.pnl / city_tbl.cost).round(3)
    city_tbl = city_tbl.merge(
        ctrl_city[["bot_share_city", "lookalike"]].reset_index()
                 .rename(columns={"index": "city"}),
        on="city", how="left")
    city_tbl["bot_share_city"] = city_tbl.bot_share_city.fillna(1.0)
    city_tbl["pnl_adj"] = (city_tbl.pnl * city_tbl.bot_share_city).round(2)

    heat = (bot.groupby(["city", "date"])["count"].sum().reset_index()
               .rename(columns={"count": "contracts"}))

    fblend = (f.groupby(["city", "date"])["err1"].mean().reset_index())
    fcity = (fblend.groupby("city")["err1"]
             .agg(bias="mean", mae=lambda x: x.abs().mean(), sd="std")
             .round(2).reset_index())

    # tail-day share per city: events where the low escaped all four buckets
    ev = settled[settled["date"].between(EST_START, EST_END)]
    ev_out = (ev.groupby(["city", "date"])
                .apply(lambda g: not (g[g.strike_type.eq("between")].result.eq("yes")).any(),
                       include_groups=False)
                .rename("tail_day").reset_index())
    tail_share = (ev_out.groupby("city").tail_day.mean().round(3).reset_index()
                  .rename(columns={"tail_day": "tail_day_share"}))

    strict_pnl = float(bot.maker_no_pnl.sum())
    strict_cost = float(bot.maker_no_cost.sum())
    payload = {
        "generated": datetime.utcnow().strftime("%Y-%m-%d %H:%MZ"),
        "era": [str(EST_START), str(EST_END)],
        "controls": controls,
        "adjusted": {
            "pnl": round(strict_pnl * controls["bot_share"], 2),
            "cost": round(strict_cost * controls["bot_share"], 2),
            "contracts": int(bot["count"].sum() * controls["bot_share"]),
            "note": "strict estimate scaled by the Thursday-control bot share",
            # if look-alike flow performs like the controls (~breakeven),
            # the whole strict loss belongs to the bot's smaller cost base
            "pnl_pessimistic": round(
                strict_pnl - strict_cost * (1 - controls["bot_share"])
                * (controls["lookalike_roi_combined"] or 0.0), 2),
            "roi_range": [
                round(strict_pnl / strict_cost, 3),
                round((strict_pnl - strict_cost * (1 - controls["bot_share"])
                       * (controls["lookalike_roi_combined"] or 0.0))
                      / (strict_cost * controls["bot_share"]), 3),
            ],
        },
        "open_exposure": round(float(open_pos.maker_no_cost.sum()), 2),
        "totals": {
            "pnl": round(strict_pnl, 2),
            "cost": round(strict_cost, 2),
            "roi": round(strict_pnl / strict_cost, 4) if strict_cost else None,
            "contracts": int(bot["count"].sum()),
            "trades": int(len(bot)),
            "markets": int(bot.ticker.nunique()),
            "days_with_fills": int(bot.date.nunique()),
            "days_live": int((EST_END - EST_START).days) + 1,
            "win_rate_mkts": round(float(per_mkt.result.eq("no").mean()), 3),
            "max_drawdown": round(max_dd, 2),
        },
        "tail_share": tail_share.to_dict("records"),
        "daily": [{"date": str(r.date), "pnl": round(r.pnl, 2),
                   "cost": round(r.cost, 2), "contracts": int(r.contracts),
                   "cum": round(r.cum, 2)} for r in daily.itertuples()],
        "by_city": [{"city": r.city, "pnl": round(r.pnl, 2), "cost": round(r.cost, 2),
                     "contracts": int(r.contracts), "mkts": int(r.mkts),
                     "wins": int(r.wins), "win_rate": r.win_rate,
                     "avg_no": round(r.avg_no, 1), "roi": r.roi,
                     "bot_share": round(float(r.bot_share_city), 2),
                     "pnl_adj": r.pnl_adj}
                    for r in city_tbl.itertuples()],
        "sessions": sess.round(2).to_dict("records"),
        "price_edge": pe.round(3).to_dict("records"),
        "weekly": wkly.round(2).to_dict("records"),
        "sensitivity": sens.to_dict("records"),
        "heat": [{"city": r.city, "date": str(r.date), "n": int(r.contracts)}
                 for r in heat.itertuples()],
        "forecast": fcity.to_dict("records"),
        "maker_class": klass.round(2).to_dict("records"),
    }
    def _clean(o):
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_clean(v) for v in o]
        if isinstance(o, (np.floating, float)):
            return None if (np.isnan(o) or np.isinf(o)) else float(o)
        if isinstance(o, np.integer):
            return int(o)
        return o

    with open(os.path.join(OUT, "dashboard_data.json"), "w") as fjs:
        json.dump(_clean(payload), fjs, indent=1)
    print(f"\ndashboard payload -> {os.path.join(OUT, 'dashboard_data.json')}")

    print("\nby city (bot-like):")
    print(by_city.to_string(index=False))
    print("\nsession split:")
    print(sess.to_string(index=False))
    print("\nprice edge:")
    print(pe.to_string(index=False))
    print("\nsensitivity:")
    print(sens.to_string(index=False))
    print("\nby day tail:")
    print(by_day.tail(12).to_string(index=False))


if __name__ == "__main__":
    main()
