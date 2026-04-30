"""KXHIGH orderbook logger.

Polls Kalshi for the orderbook of every open KXHIGH market, every 15 min,
and appends a row to BigQuery `KXHIGH_orderbook_log`. Used as the data
foundation for two future analyses:

1. **Increment tuning** ("would 1¢ ladder rungs catch more fills than 2¢?"):
   reconstruct the no_lowest_offer trajectory per market over its lifetime
   and ask "did the offer ever touch <integer cent>?". Each touch is a
   hypothetical fill for a rung at that price.

2. **Cancel-time tuning** ("what if we kept orders open until X?"):
   for each canceled order, compare the cancel timestamp to the times
   the market subsequently dipped to the order's bid price. Missed fills
   = orders we'd have caught with later cancel times.

Schema (KXHIGH_orderbook_log, partitioned by polled_at, clustered by
market_ticker):
  polled_at          TIMESTAMP   when we polled
  market_ticker      STRING
  event_ticker       STRING
  city_abv           STRING      e.g. MIA, THOU, TBOS
  event_date         DATE        target high date
  bucket_type        STRING      B (between) or T (tail)
  bucket_val         FLOAT       e.g. 79.5 for B79.5
  market_status      STRING      open / closed / settled
  no_highest_bid     INT64       best NO bid (¢)
  no_lowest_offer    INT64       best NO offer (¢, derived from yes bids)
  yes_highest_bid    INT64       best YES bid (¢)
  yes_lowest_offer   INT64       best YES offer (¢)
  spread_c           INT64       no_lowest_offer − no_highest_bid
  n_no_levels        INT64
  n_yes_levels       INT64
  no_levels_json     STRING      full NO orderbook depth (JSON)
  yes_levels_json    STRING      full YES orderbook depth (JSON)

Run via GitHub Actions cron `*/15 * * * *` (every 15 minutes).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

import pandas as pd
from google.cloud import bigquery

from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient

PROJECT = os.environ.get("BQ_PROJECT", "elite-contact-446323-q7")
DATASET = os.environ.get("BQ_DATASET", "Kalshi")
TABLE = f"{PROJECT}.{DATASET}.KXHIGH_orderbook_log"
NWS_OBS_TABLE = f"{PROJECT}.{DATASET}.KXHIGH_nws_obs_log"
NWS_FCST_TABLE = f"{PROJECT}.{DATASET}.KXHIGH_nws_hourly_forecast"
POSITIONS_TABLE = f"{PROJECT}.{DATASET}.KXHIGH_positions_log"
FILLS_LIVE_TABLE = f"{PROJECT}.{DATASET}.KXHIGH_fills_live"
LOCATION = "northamerica-northeast1"

# Cache the NWS gridpoint hourly-forecast URL per (lat, lon). NWS gridpoints
# are stable for a given coordinate, so we only need to look them up once per
# process — saves 20 redundant /points calls every cron tick.
_NWS_HOURLY_URL_CACHE: Dict[Tuple[float, float], str] = {}

# Pull the canonical city → ICAO map from the analysis package so we
# share one source of truth (lat/lon, station identifier).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "analysis", "kxhigh", "python"))
try:
    from cities import CITIES as _CITIES  # noqa: E402
except Exception as _e:
    print(f"warn: could not import cities map ({_e}); NWS obs polling disabled")
    _CITIES = {}

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")

TICKER_RE = re.compile(r"^KXHIGH([A-Z]+)-26([A-Z]+)(\d+)-([BT])(\d+\.?\d*)$")


def _load_private_key():
    """Match the same env conventions as fetch_settlements_csv / trading bot."""
    pem_b64 = os.environ.get("KALSHI_PRIVATE_KEY")
    if pem_b64:
        try:
            pem = base64.b64decode(pem_b64)
        except Exception:
            pem = pem_b64.encode()
        return serialization.load_pem_private_key(pem, password=None, backend=default_backend())
    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt")
    if not os.path.exists(pem_path):
        raise FileNotFoundError(
            f"No Kalshi private key. Set KALSHI_PRIVATE_KEY (b64) or place PEM at {pem_path!r}."
        )
    with open(pem_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


def _parse_ticker(ticker: str) -> Optional[Dict[str, Any]]:
    """Parse KXHIGH ticker into structured fields. Returns None if mismatch."""
    m = TICKER_RE.match(ticker)
    if not m:
        return None
    abv, mon, dd, btype, bval = m.group(1), m.group(2), int(m.group(3)), m.group(4), float(m.group(5))
    try:
        ed = datetime.strptime(f"26{mon}{dd:02d}", "%y%b%d").date()
    except ValueError:
        return None
    return {
        "city_abv": abv,
        "event_ticker": f"KXHIGH{abv}-26{mon}{dd:02d}",
        "event_date": ed,
        "bucket_type": btype,
        "bucket_val": bval,
    }


def _orderbook_to_levels(orderbook_response: dict) -> Tuple[List[List[int]], List[List[int]]]:
    """Normalize Kalshi orderbook response (handles old + new format)."""
    no_levels: List[List[int]] = []
    yes_levels: List[List[int]] = []
    if "orderbook_fp" in orderbook_response:
        ob_fp = orderbook_response["orderbook_fp"] or {}
        for level in ob_fp.get("no_dollars", []) or []:
            no_levels.append([int(round(float(level[0]) * 100)), int(float(level[1]))])
        for level in ob_fp.get("yes_dollars", []) or []:
            yes_levels.append([int(round(float(level[0]) * 100)), int(float(level[1]))])
    elif "orderbook" in orderbook_response:
        ob = orderbook_response["orderbook"] or {}
        no_levels = ob.get("no", []) or []
        yes_levels = ob.get("yes", []) or []
    else:
        no_levels = orderbook_response.get("no", []) or []
        yes_levels = orderbook_response.get("yes", []) or []
    return no_levels, yes_levels


# 20 city event-ticker prefixes matching high_temp_trading.py:728-747.
# Each city has its own daily event; we enumerate today + tomorrow + day-after
# so we cover whatever's currently tradeable.
CITY_EVENT_PREFIXES = [
    "KXHIGHCHI", "KXHIGHNY", "KXHIGHDEN", "KXHIGHPHIL", "KXHIGHAUS",
    "KXHIGHMIA", "KXHIGHTHOU", "KXHIGHLAX", "KXHIGHTATL", "KXHIGHTDC",
    "KXHIGHTPHX", "KXHIGHTDAL", "KXHIGHTLV", "KXHIGHTOKC", "KXHIGHTSEA",
    "KXHIGHTSFO", "KXHIGHTSATX", "KXHIGHTMIN", "KXHIGHTNOLA", "KXHIGHTBOS",
]


def list_open_kxhigh_markets(client: ExchangeClient) -> List[Dict[str, Any]]:
    """Enumerate today/tomorrow/day-after events per city and collect their
    open markets. Mirrors high_temp_trading's pattern (get_event per ticker)."""
    import pytz
    from datetime import timedelta as _td
    central = pytz.timezone("US/Central")
    today = datetime.now(central).date()
    candidate_dates = [today, today + _td(days=1), today + _td(days=2)]
    out: List[Dict[str, Any]] = []
    seen_tickers = set()
    for d in candidate_dates:
        date_code = d.strftime("%y%b%d").upper().lstrip("0")
        # event ticker format: KXHIGHCHI-26APR25 (no leading zero on day for some cities,
        # but actually let's match the on-the-wire format used by the trading script)
        date_code = "26" + d.strftime("%b").upper() + d.strftime("%d")
        for prefix in CITY_EVENT_PREFIXES:
            ev_ticker = f"{prefix}-{date_code}"
            try:
                resp = client.get_event(event_ticker=ev_ticker)
            except Exception:
                continue  # event for that day doesn't exist
            for m in resp.get("markets", []) or []:
                tk = m.get("ticker")
                if not tk or tk in seen_tickers:
                    continue
                # Kalshi returns "active" for tradeable markets, sometimes "open"
                if m.get("status") not in ("open", "active"):
                    continue
                seen_tickers.add(tk)
                out.append(m)
    return out


def ensure_table(client: bigquery.Client) -> None:
    schema = [
        bigquery.SchemaField("polled_at",        "TIMESTAMP", "REQUIRED"),
        bigquery.SchemaField("market_ticker",    "STRING",    "REQUIRED"),
        bigquery.SchemaField("event_ticker",     "STRING"),
        bigquery.SchemaField("city_abv",         "STRING"),
        bigquery.SchemaField("event_date",       "DATE"),
        bigquery.SchemaField("bucket_type",      "STRING"),
        bigquery.SchemaField("bucket_val",       "FLOAT"),
        bigquery.SchemaField("market_status",    "STRING"),
        bigquery.SchemaField("no_highest_bid",   "INTEGER"),
        bigquery.SchemaField("no_lowest_offer",  "INTEGER"),
        bigquery.SchemaField("yes_highest_bid",  "INTEGER"),
        bigquery.SchemaField("yes_lowest_offer", "INTEGER"),
        bigquery.SchemaField("spread_c",         "INTEGER"),
        bigquery.SchemaField("n_no_levels",      "INTEGER"),
        bigquery.SchemaField("n_yes_levels",     "INTEGER"),
        bigquery.SchemaField("no_levels_json",   "STRING"),
        bigquery.SchemaField("yes_levels_json",  "STRING"),
    ]
    table = bigquery.Table(TABLE, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="polled_at",
    )
    table.clustering_fields = ["market_ticker"]
    try:
        client.create_table(table)
        print(f"Created {TABLE}")
    except Exception:
        pass  # already exists


def ensure_nws_obs_table(client: bigquery.Client) -> None:
    schema = [
        bigquery.SchemaField("polled_at",         "TIMESTAMP", "REQUIRED"),
        bigquery.SchemaField("city_abv",          "STRING",    "REQUIRED"),
        bigquery.SchemaField("station",           "STRING"),
        bigquery.SchemaField("observation_time",  "TIMESTAMP"),
        bigquery.SchemaField("temperature_f",     "FLOAT"),
        bigquery.SchemaField("text_description",  "STRING"),
        bigquery.SchemaField("error",             "STRING"),
    ]
    table = bigquery.Table(NWS_OBS_TABLE, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="polled_at"
    )
    table.clustering_fields = ["city_abv"]
    try:
        client.create_table(table)
        print(f"Created {NWS_OBS_TABLE}")
    except Exception:
        pass  # already exists


def _nws_get_latest_obs(station: str, timeout: int = 8) -> Dict[str, Any]:
    """Return latest NWS observation for a station ICAO (KNYC, KMIA, …).

    Mirrors the pattern in high_temp_trading.py:_nws_get/_get_obs without the
    full retry harness — we run on a 30-min cron so a single attempt is fine
    and a transient miss recovers next tick.
    """
    import urllib.request
    url = f"https://api.weather.gov/stations/{station}/observations/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/geo+json",
                                               "User-Agent": "kxhigh-bot (kxhigh@local)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    props = body.get("properties", {}) or {}
    temp_c = (props.get("temperature") or {}).get("value")
    temp_f = (temp_c * 9.0 / 5.0 + 32.0) if temp_c is not None else None
    return {
        "observation_time": props.get("timestamp"),  # ISO string
        "temperature_f": temp_f,
        "text_description": props.get("textDescription"),
    }


def poll_and_write_nws_obs(bq_client: bigquery.Client, poll_ts: datetime) -> int:
    """Poll the NWS station for each city in CITIES and append rows.

    Returns the number of rows written. Failures per-city become rows with the
    `error` column populated so we can inspect coverage gaps later.
    """
    if not _CITIES:
        return 0
    ensure_nws_obs_table(bq_client)
    rows: List[Dict[str, Any]] = []
    for city_abv, meta in _CITIES.items():
        station = meta.get("icao")
        if not station:
            continue
        row: Dict[str, Any] = {
            "polled_at": poll_ts,
            "city_abv": city_abv,
            "station": station,
            "observation_time": None,
            "temperature_f": None,
            "text_description": None,
            "error": None,
        }
        try:
            obs = _nws_get_latest_obs(station)
            row["observation_time"] = obs["observation_time"]
            row["temperature_f"] = obs["temperature_f"]
            row["text_description"] = obs["text_description"]
        except Exception as e:
            row["error"] = str(e)[:200]
        rows.append(row)
        time.sleep(0.05)  # polite pacing — same convention as the orderbook poll
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["polled_at"] = pd.to_datetime(df["polled_at"], utc=True)
    df["observation_time"] = pd.to_datetime(df["observation_time"], utc=True, errors="coerce")
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND",
        schema_update_options=["ALLOW_FIELD_ADDITION"],
    )
    bq_client.load_table_from_dataframe(df, NWS_OBS_TABLE, job_config=job_config).result()
    n_ok = int(df["temperature_f"].notna().sum())
    print(f"  NWS obs: {n_ok}/{len(df)} stations returned a temperature")
    return len(df)


def ensure_nws_fcst_table(client: bigquery.Client) -> None:
    schema = [
        bigquery.SchemaField("polled_at",          "TIMESTAMP", "REQUIRED"),
        bigquery.SchemaField("city_abv",           "STRING",    "REQUIRED"),
        bigquery.SchemaField("station",            "STRING"),
        bigquery.SchemaField("forecast_update_ts", "TIMESTAMP"),  # NWS-side update time
        bigquery.SchemaField("period_start",       "TIMESTAMP"),  # the forecast hour
        bigquery.SchemaField("period_end",         "TIMESTAMP"),
        bigquery.SchemaField("temperature_f",      "FLOAT"),
        bigquery.SchemaField("short_forecast",     "STRING"),
    ]
    table = bigquery.Table(NWS_FCST_TABLE, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="polled_at"
    )
    table.clustering_fields = ["city_abv", "period_start"]
    try:
        client.create_table(table)
        print(f"Created {NWS_FCST_TABLE}")
    except Exception:
        pass  # already exists


def _nws_hourly_url(lat: float, lon: float, timeout: int = 8) -> Optional[str]:
    """Resolve a coordinate to its hourly-forecast URL via /points. Cached."""
    key = (round(lat, 4), round(lon, 4))
    if key in _NWS_HOURLY_URL_CACHE:
        return _NWS_HOURLY_URL_CACHE[key]
    import urllib.request
    url = f"https://api.weather.gov/points/{lat},{lon}"
    req = urllib.request.Request(url, headers={"Accept": "application/geo+json",
                                               "User-Agent": "kxhigh-bot (kxhigh@local)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        hourly = data.get("properties", {}).get("forecastHourly")
        if hourly:
            _NWS_HOURLY_URL_CACHE[key] = hourly
        return hourly
    except Exception as e:
        print(f"  /points lookup failed for {lat},{lon}: {e}")
        return None


def _nws_get_hourly_forecast(hourly_url: str, timeout: int = 12) -> Dict[str, Any]:
    import urllib.request
    req = urllib.request.Request(hourly_url, headers={"Accept": "application/geo+json",
                                                       "User-Agent": "kxhigh-bot (kxhigh@local)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    props = body.get("properties", {}) or {}
    return {
        "forecast_update_ts": props.get("updateTime"),
        "periods": props.get("periods", []) or [],
    }


def poll_and_write_hourly_forecast(bq_client: bigquery.Client, poll_ts: datetime) -> int:
    """Persist NWS hourly forecast for every tracked city, today+tomorrow only.

    Two API calls per city per tick: one /points lookup (cached after first
    call) and one hourly-forecast pull (~156 periods returned, we keep ~48).
    Failures don't write rows for that city; obs and orderbook still proceed.
    """
    if not _CITIES:
        return 0
    ensure_nws_fcst_table(bq_client)
    from datetime import timedelta
    horizon_end = (poll_ts + timedelta(hours=48)).replace(tzinfo=timezone.utc)
    rows: List[Dict[str, Any]] = []
    for city_abv, meta in _CITIES.items():
        lat, lon = meta.get("lat"), meta.get("lon")
        station = meta.get("icao")
        if lat is None or lon is None:
            continue
        hourly_url = _nws_hourly_url(lat, lon)
        if not hourly_url:
            continue
        try:
            fcst = _nws_get_hourly_forecast(hourly_url)
        except Exception as e:
            print(f"  hourly forecast failed for {city_abv}: {e}")
            continue
        update_ts = fcst.get("forecast_update_ts")
        for p in fcst["periods"]:
            try:
                ps = pd.Timestamp(p.get("startTime")).tz_convert("UTC")
            except Exception:
                continue
            if ps.to_pydatetime() > horizon_end:
                break  # periods are time-ordered; future-truncate
            try:
                pe = pd.Timestamp(p.get("endTime")).tz_convert("UTC").to_pydatetime()
            except Exception:
                pe = None
            temp = p.get("temperature")
            if p.get("temperatureUnit") == "C" and temp is not None:
                temp = temp * 9.0 / 5.0 + 32.0
            rows.append({
                "polled_at":          poll_ts,
                "city_abv":           city_abv,
                "station":            station,
                "forecast_update_ts": update_ts,
                "period_start":       ps.to_pydatetime(),
                "period_end":         pe,
                "temperature_f":      float(temp) if temp is not None else None,
                "short_forecast":     p.get("shortForecast"),
            })
        time.sleep(0.05)
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["polled_at"] = pd.to_datetime(df["polled_at"], utc=True)
    df["forecast_update_ts"] = pd.to_datetime(df["forecast_update_ts"], utc=True, errors="coerce")
    df["period_start"] = pd.to_datetime(df["period_start"], utc=True, errors="coerce")
    df["period_end"] = pd.to_datetime(df["period_end"], utc=True, errors="coerce")
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND",
        schema_update_options=["ALLOW_FIELD_ADDITION"],
    )
    bq_client.load_table_from_dataframe(df, NWS_FCST_TABLE, job_config=job_config).result()
    n_cities = df["city_abv"].nunique()
    print(f"  hourly forecast: {len(df)} rows across {n_cities} cities")
    return len(df)


def ensure_positions_table(client: bigquery.Client) -> None:
    """Schema mirrors the Kalshi API response so a row is a faithful snapshot.

    Kalshi returns `position_fp` as a signed string-encoded float (negative for
    NO-side inventory) and the dollar amounts as decimal-string fields. We
    parse and store as native types here so downstream readers don't need to
    redo the conversion.
    """
    schema = [
        bigquery.SchemaField("polled_at",                "TIMESTAMP", "REQUIRED"),
        bigquery.SchemaField("market_ticker",            "STRING",    "REQUIRED"),
        bigquery.SchemaField("position",                 "FLOAT"),    # signed; − = NO, + = YES
        bigquery.SchemaField("market_exposure_dollars",  "FLOAT"),
        bigquery.SchemaField("realized_pnl_dollars",     "FLOAT"),
        bigquery.SchemaField("total_traded_dollars",     "FLOAT"),
        bigquery.SchemaField("fees_paid_dollars",        "FLOAT"),
        bigquery.SchemaField("resting_orders_count",     "INTEGER"),
        bigquery.SchemaField("last_updated_ts",          "TIMESTAMP"),
    ]
    table = bigquery.Table(POSITIONS_TABLE, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="polled_at"
    )
    table.clustering_fields = ["market_ticker"]
    try:
        client.create_table(table)
        print(f"Created {POSITIONS_TABLE}")
    except Exception:
        pass  # already exists


def _to_float(s: Any) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def poll_and_write_positions(
    bq_client: bigquery.Client,
    ex,
    poll_ts: datetime,
) -> int:
    """Snapshot every KXHIGH market_position from Kalshi.

    Pages through `/portfolio/positions` with no settlement filter (Kalshi
    returns dollars-encoded fields like `market_exposure_dollars` and
    `position_fp`; an `unsettled` filter would also drop positions on a market
    that just closed but hasn't paid out yet, which we still want to surface
    while the cash is locked up).
    """
    ensure_positions_table(bq_client)
    rows: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        kwargs: Dict[str, Any] = {"limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = ex.get_positions(**kwargs)
        except Exception as e:
            print(f"  positions poll failed (page {pages}): {e}")
            break
        market_positions = resp.get("market_positions", []) or []
        for mp in market_positions:
            ticker = mp.get("ticker") or mp.get("market_ticker")
            if not ticker or not ticker.startswith("KXHIGH"):
                continue
            rows.append({
                "polled_at":               poll_ts,
                "market_ticker":           ticker,
                "position":                _to_float(mp.get("position_fp")),
                "market_exposure_dollars": _to_float(mp.get("market_exposure_dollars")),
                "realized_pnl_dollars":    _to_float(mp.get("realized_pnl_dollars")),
                "total_traded_dollars":    _to_float(mp.get("total_traded_dollars")),
                "fees_paid_dollars":       _to_float(mp.get("fees_paid_dollars")),
                "resting_orders_count":    mp.get("resting_orders_count"),
                "last_updated_ts":         mp.get("last_updated_ts"),
            })
        cursor = resp.get("cursor") or None
        pages += 1
        if not cursor or pages >= 10:
            break
    if not rows:
        print("  no KXHIGH positions to log")
        return 0
    df = pd.DataFrame(rows)
    df["polled_at"] = pd.to_datetime(df["polled_at"], utc=True)
    df["last_updated_ts"] = pd.to_datetime(df["last_updated_ts"], utc=True, errors="coerce")
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND",
        schema_update_options=["ALLOW_FIELD_ADDITION"],
    )
    bq_client.load_table_from_dataframe(df, POSITIONS_TABLE, job_config=job_config).result()
    n_open = int((df["position"].fillna(0) != 0).sum())
    total_exp = float(df["market_exposure_dollars"].fillna(0).sum())
    print(f"  positions: {len(df)} markets logged ({n_open} non-zero, ${total_exp:.2f} total exposure)")
    return len(df)


def ensure_fills_live_table(client: bigquery.Client) -> None:
    """Live fills table — APPENDed every cron tick. Distinct from
    KXHIGH_fills which is TRUNCATEd nightly by high_temp_monitoring.py.
    The dashboard queries this table for fill-rate so the number stays
    fresh (fills_live updates every 30 min instead of once a day).
    """
    schema = [
        bigquery.SchemaField("polled_at",        "TIMESTAMP", "REQUIRED"),
        bigquery.SchemaField("fill_id",          "STRING",    "REQUIRED"),
        bigquery.SchemaField("order_id",         "STRING"),
        bigquery.SchemaField("market_ticker",    "STRING"),
        bigquery.SchemaField("count_fp",         "FLOAT"),
        bigquery.SchemaField("side",             "STRING"),
        bigquery.SchemaField("action",           "STRING"),
        bigquery.SchemaField("no_price_dollars", "FLOAT"),
        bigquery.SchemaField("yes_price_dollars","FLOAT"),
        bigquery.SchemaField("is_taker",         "BOOLEAN"),
        bigquery.SchemaField("created_time",     "TIMESTAMP"),
    ]
    table = bigquery.Table(FILLS_LIVE_TABLE, schema=schema)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="polled_at"
    )
    table.clustering_fields = ["market_ticker", "order_id"]
    try:
        client.create_table(table)
        print(f"Created {FILLS_LIVE_TABLE}")
    except Exception:
        pass  # already exists


def poll_and_write_fills(
    bq_client: bigquery.Client,
    ex,
    poll_ts: datetime,
    *,
    lookback_hours: int = 36,
) -> int:
    """Pull recent KXHIGH fills from Kalshi and APPEND.

    The lookback is wide enough to catch yesterday-evening's pre-market
    runs (which place orders that fill overnight, before the next cron
    tick can record them). Duplicates across ticks are fine — we APPEND
    by polled_at and dedupe at query time on fill_id.
    """
    ensure_fills_live_table(bq_client)
    from datetime import timedelta as _td
    min_ts = int((poll_ts - _td(hours=lookback_hours)).timestamp())
    rows: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    pages = 0
    while True:
        kwargs: Dict[str, Any] = {"limit": 1000, "min_ts": min_ts}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = ex.get_fills(**kwargs)
        except Exception as e:
            print(f"  fills poll failed (page {pages}): {e}")
            break
        fills = resp.get("fills", []) or []
        for f in fills:
            ticker = f.get("ticker") or f.get("market_ticker")
            if not ticker or not ticker.startswith("KXHIGH"):
                continue
            rows.append({
                "polled_at":         poll_ts,
                "fill_id":           f.get("trade_id") or f.get("fill_id"),
                "order_id":          f.get("order_id"),
                "market_ticker":     ticker,
                "count_fp":          _to_float(f.get("count")) if f.get("count") is not None else _to_float(f.get("count_fp")),
                "side":              f.get("side"),
                "action":            f.get("action"),
                "no_price_dollars":  _to_float(f.get("no_price")) if f.get("no_price") is not None else _to_float(f.get("no_price_dollars")),
                "yes_price_dollars": _to_float(f.get("yes_price")) if f.get("yes_price") is not None else _to_float(f.get("yes_price_dollars")),
                "is_taker":          bool(f.get("is_taker")) if f.get("is_taker") is not None else None,
                "created_time":      f.get("created_time"),
            })
        cursor = resp.get("cursor") or None
        pages += 1
        if not cursor or pages >= 20:
            break
    # Drop rows missing fill_id (required column).
    rows = [r for r in rows if r.get("fill_id")]
    if not rows:
        print("  no KXHIGH fills to log")
        return 0
    df = pd.DataFrame(rows)
    df["polled_at"] = pd.to_datetime(df["polled_at"], utc=True)
    df["created_time"] = pd.to_datetime(df["created_time"], utc=True, errors="coerce")
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND",
        schema_update_options=["ALLOW_FIELD_ADDITION"],
    )
    bq_client.load_table_from_dataframe(df, FILLS_LIVE_TABLE, job_config=job_config).result()
    print(f"  fills: {len(df)} rows logged (lookback {lookback_hours}h)")
    return len(df)


def write_run_marker(bq_client, event, run_id, started_at, **fields):
    """Append one row to KXHIGH_runs (shared with high_temp_trading)."""
    import uuid as _uuid
    table_id = f"{PROJECT}.{DATASET}.KXHIGH_runs"
    row = {
        "run_id": run_id,
        "event": event,
        "event_at": datetime.now(timezone.utc),
        "started_at": started_at,
        "script_name": "kxhigh_orderbook_logger.py",
        "workflow_name": os.environ.get("GITHUB_WORKFLOW"),
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "runner_os": os.environ.get("RUNNER_OS"),
    }
    row.update(fields)
    try:
        df = pd.DataFrame([row])
        job_config = bigquery.LoadJobConfig(
            write_disposition="WRITE_APPEND",
            schema_update_options=["ALLOW_FIELD_ADDITION"],
        )
        bq_client.load_table_from_dataframe(df, table_id, job_config=job_config).result()
    except Exception as e:
        print(f"  RUNS marker fail ({event}): {e}")


def main():
    import uuid
    run_id = str(uuid.uuid4())
    poll_ts = datetime.now(timezone.utc)
    print(f"[{poll_ts.isoformat()}] KXHIGH orderbook poll starting (run_id={run_id})")

    private_key = _load_private_key()
    ex = ExchangeClient(exchange_api_base=KALSHI_API_BASE, key_id=KEY_ID, private_key=private_key)

    bq_client = bigquery.Client(project=PROJECT)
    write_run_marker(bq_client, "start", run_id, poll_ts)

    # 0a. Snapshot NWS station obs for every tracked city (one row per station,
    #     even on failure, with `error` populated). Cheap and decoupled from
    #     the Kalshi calls so a Kalshi outage still leaves us with a temp curve.
    n_obs = 0
    try:
        n_obs = poll_and_write_nws_obs(bq_client, poll_ts)
    except Exception as e:
        print(f"  NWS obs poll failed: {e}")

    # 0a'. NWS hourly forecast for the next ~48h per city. Pairs with obs to
    #      give the dashboard a real "actuals vs forecast" intraday curve.
    n_fcst = 0
    try:
        n_fcst = poll_and_write_hourly_forecast(bq_client, poll_ts)
    except Exception as e:
        print(f"  NWS hourly-forecast poll failed: {e}")

    # 0b. Snapshot live KXHIGH positions from Kalshi. Authoritative source for
    #     "current exposure" — supersedes the trading bot's once-per-run
    #     snapshot.position field, which is unsigned and stale between runs.
    n_pos = 0
    try:
        n_pos = poll_and_write_positions(bq_client, ex, poll_ts)
    except Exception as e:
        print(f"  positions poll failed: {e}")

    # 0c. Poll recent fills (last 36h) so the dashboard's fill-rate stays
    #     fresh. The base KXHIGH_fills table only refreshes nightly via
    #     high_temp_monitoring.py — fills_live closes that gap.
    n_fills = 0
    try:
        n_fills = poll_and_write_fills(bq_client, ex, poll_ts)
    except Exception as e:
        print(f"  fills poll failed: {e}")

    # 1. List open KXHIGH markets
    markets = list_open_kxhigh_markets(ex)
    print(f"  found {len(markets)} open KXHIGH markets")
    if not markets:
        print("  nothing to log; exiting")
        write_run_marker(bq_client, "end", run_id, poll_ts,
                         finished_at=datetime.now(timezone.utc),
                         n_markets_seen=0, n_orderbook_rows=0,
                         duration_seconds=(datetime.now(timezone.utc)-poll_ts).total_seconds(),
                         exit_status="empty_market_list")
        return

    ensure_table(bq_client)

    # 2. Poll orderbook per market and build rows
    rows: List[Dict[str, Any]] = []
    fails = 0
    for i, m in enumerate(markets):
        ticker = m.get("ticker")
        if not ticker:
            continue
        parsed = _parse_ticker(ticker) or {
            "city_abv": None, "event_ticker": None, "event_date": None,
            "bucket_type": None, "bucket_val": None,
        }
        try:
            ob = ex.get_orderbook(ticker=ticker)
        except Exception as e:
            fails += 1
            if fails < 5:
                print(f"  ! orderbook fail for {ticker}: {e}")
            continue
        no_levels, yes_levels = _orderbook_to_levels(ob)
        no_bid = no_levels[-1][0] if no_levels else None
        yes_bid = yes_levels[-1][0] if yes_levels else None
        # NO offer is implied by the highest YES bid (Kalshi convention)
        no_offer = (100 - yes_bid) if yes_bid is not None else None
        yes_offer = (100 - no_bid) if no_bid is not None else None
        spread = (no_offer - no_bid) if (no_bid is not None and no_offer is not None) else None
        rows.append({
            "polled_at": poll_ts,
            "market_ticker": ticker,
            "event_ticker": parsed["event_ticker"],
            "city_abv": parsed["city_abv"],
            "event_date": parsed["event_date"],
            "bucket_type": parsed["bucket_type"],
            "bucket_val": parsed["bucket_val"],
            "market_status": m.get("status"),
            "no_highest_bid": no_bid,
            "no_lowest_offer": no_offer,
            "yes_highest_bid": yes_bid,
            "yes_lowest_offer": yes_offer,
            "spread_c": spread,
            "n_no_levels": len(no_levels),
            "n_yes_levels": len(yes_levels),
            "no_levels_json": json.dumps(no_levels),
            "yes_levels_json": json.dumps(yes_levels),
        })
        # gentle pacing (~5 markets/s; Kalshi public endpoint allows 100/s but
        # we don't need fast and want to be polite)
        time.sleep(0.05)

    print(f"  collected {len(rows)} orderbook rows ({fails} failures)")
    if not rows:
        write_run_marker(bq_client, "end", run_id, poll_ts,
                         finished_at=datetime.now(timezone.utc),
                         n_markets_seen=len(markets), n_orderbook_rows=0,
                         n_fails=fails,
                         duration_seconds=(datetime.now(timezone.utc)-poll_ts).total_seconds(),
                         exit_status="no_rows_collected")
        return

    # 3. Append to BQ
    df = pd.DataFrame(rows)
    df["polled_at"] = pd.to_datetime(df["polled_at"], utc=True)
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_APPEND",
        schema_update_options=["ALLOW_FIELD_ADDITION"],
    )
    job = bq_client.load_table_from_dataframe(df, TABLE, job_config=job_config)
    job.result()
    print(f"  wrote {len(df)} rows -> {TABLE}")
    write_run_marker(bq_client, "end", run_id, poll_ts,
                     finished_at=datetime.now(timezone.utc),
                     n_markets_seen=len(markets), n_orderbook_rows=len(df),
                     n_fails=fails, n_nws_obs_rows=n_obs,
                     n_nws_fcst_rows=n_fcst, n_positions_rows=n_pos,
                     duration_seconds=(datetime.now(timezone.utc)-poll_ts).total_seconds(),
                     exit_status="success")


if __name__ == "__main__":
    main()
