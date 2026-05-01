#!/usr/bin/env python3
"""Fetch platform-wide volume_fp for KXHIGH markets and write kxhigh_volume_cache.json.

Reads market tickers from a settlements CSV (already fetched), calls the
Kalshi markets endpoint once per ticker to get volume_fp (total contracts
traded platform-wide for that market), aggregates by city + event date,
and writes the result as JSON.

Usage:
    python fetch_kxhigh_volume.py <settlements.csv> [output.json]

Defaults: latest kalshi_daily/ settlements CSV, output to kxhigh_volume_cache.json.
"""

import os, csv, json, time, base64, sys, glob
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient

LOOKBACK_DAYS = 60


def load_private_key(b64_key='', file_path=''):
    if file_path and os.path.exists(file_path):
        with open(file_path, 'rb') as f: pem = f.read()
    elif b64_key:
        try: pem = base64.b64decode(b64_key)
        except Exception: pem = b64_key.encode()
    else:
        raise FileNotFoundError(f'No private key. Set KALSHI_PRIVATE_KEY or place PEM at {file_path!r}.')
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())


def parse_ticker(ticker):
    """KXHIGHLAX-26APR25-B64.5 → ('LAX', '2026-04-25') or (None, None)."""
    try:
        parts = ticker.split('-')
        city = parts[0].replace('KXHIGH', '')
        dt = datetime.strptime(parts[1], '%y%b%d')
        return city, dt.strftime('%Y-%m-%d')
    except Exception:
        return None, None


def main():
    settlements_csv = sys.argv[1] if len(sys.argv) > 1 else None
    out_path = sys.argv[2] if len(sys.argv) > 2 else 'kxhigh_volume_cache.json'

    if not settlements_csv:
        files = sorted(glob.glob('kalshi_daily/*/Kalshi-Settlements-*.csv'))
        if not files:
            print('No settlements CSV found — pass path as first argument.')
            sys.exit(1)
        settlements_csv = files[-1]
        print(f'Using {settlements_csv}')

    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')

    unique_tickers = set()
    with open(settlements_csv, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            if row.get('type') != 'Settlement': continue
            tk = row.get('Market_Ticker', '')
            if not tk.startswith('KXHIGH'): continue
            _, date_str = parse_ticker(tk)
            if date_str and date_str >= cutoff:
                unique_tickers.add(tk)

    print(f'Fetching volume_fp for {len(unique_tickers)} KXHIGH markets (last {LOOKBACK_DAYS}d)...')

    key_id = os.environ.get('KALSHI_API_KEY_ID', 'c3204983-77fc-491b-99f7-136600698178')
    private_key = load_private_key(
        b64_key=os.environ.get('KALSHI_PRIVATE_KEY', ''),
        file_path=os.environ.get('KALSHI_PRIVATE_KEY_PATH', 'Lisa_Kalshi.txt'),
    )
    client = ExchangeClient('https://api.elections.kalshi.com/trade-api/v2', key_id, private_key)

    by_city_date = defaultdict(lambda: defaultdict(float))
    errors = 0
    tickers_sorted = sorted(unique_tickers)

    for i, ticker in enumerate(tickers_sorted):
        try:
            m = client.get_market(ticker).get('market', {})
            vol = float(m.get('volume_fp') or 0)
            city, date_str = parse_ticker(ticker)
            if city and date_str:
                by_city_date[city][date_str] += vol
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f'  error {ticker}: {e}')
        if (i + 1) % 50 == 0:
            print(f'  {i+1}/{len(tickers_sorted)} fetched...')
        time.sleep(0.05)

    result = {
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'lookback_days': LOOKBACK_DAYS,
        'by_city_date': {city: dict(sorted(dates.items())) for city, dates in sorted(by_city_date.items())},
    }

    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2)

    n_points = sum(len(d) for d in by_city_date.values())
    print(f'Wrote {out_path} — {len(by_city_date)} cities, {n_points} city-day points, {errors} errors')


if __name__ == '__main__':
    main()
