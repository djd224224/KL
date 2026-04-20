# -*- coding: utf-8 -*-
"""Pull portfolio settlements from the Kalshi API and write a CSV that matches
the format of the 'Kalshi-Recent-Activity-Settlement' export.

Uses the same auth pattern as the other scripts in this folder: env vars
KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY / KALSHI_PRIVATE_KEY_PATH, falling
back to Lisa_Kalshi.txt in the working directory.
"""

import os
import csv
import base64
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient


CSV_COLUMNS = [
    "type", "Status", "Amount_In_Dollars", "Original_Date", "Traded_Time",
    "Last_Updated", "Deposit_Type", "Fee_In_Dollars", "Market_Title",
    "Market_Ticker", "Market_Id", "Filled", "Remaining", "Direction",
    "Order_Type", "Price_In_Cents", "No_Contracts_Owned",
    "No_Contracts_Average_Price_In_Cents", "Yes_Contracts_Owned",
    "Yes_Contracts_Average_Price_In_Cents", "Result", "Profit_In_Dollars",
    "Credit_Reason", "Credit_Type", "Introducing_Broker",
]


def load_private_key(b64_key="", file_path=""):
    if file_path and os.path.exists(file_path):
        with open(file_path, "rb") as f:
            pem = f.read()
    elif b64_key:
        try:
            pem = base64.b64decode(b64_key)
        except Exception:
            pem = b64_key.encode()
    else:
        raise FileNotFoundError(
            f"No private key. Set KALSHI_PRIVATE_KEY or place the PEM at '{file_path}'."
        )
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())


def _floor_count(count_fp):
    """CSV displays the integer floor of the fractional ``_fp`` count."""
    try:
        return int(float(count_fp or 0))
    except (TypeError, ValueError):
        return 0


def fmt_count(count_fp):
    return str(_floor_count(count_fp))


def fmt_avg_price(total_cost_dollars, count_fp):
    """CSV avg = total_cost_dollars / floor(count_fp), formatted with
    Python's native f'{x:.2f}'. This reproduces the IEEE-754 / banker's
    rounding quirks in the Kalshi export (e.g. 22.80/48 = 0.475... → 0.48
    while 15.20/32 = 0.475 → 0.47). Column name says '...In_Cents' but the
    values are dollars with 2 decimals."""
    count = _floor_count(count_fp)
    if count <= 0:
        return "0.00"
    try:
        cost = float(total_cost_dollars or 0)
    except (TypeError, ValueError):
        return "0.00"
    return f"{cost / count:.2f}"


def fmt_settled_time(ts):
    """Kalshi API returns settled_time as ISO with 'Z' and variable sub-second
    precision (microseconds). The CSV export truncates to milliseconds, and
    drops the fractional portion entirely when milliseconds == 0."""
    if not ts:
        return ""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    ms = dt.microsecond // 1000
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}Z" if ms == 0 else f"{base}.{ms:03d}Z"


def row_from_settlement(s):
    return {
        "type": "Settlement",
        "Status": "",
        "Amount_In_Dollars": "",
        "Original_Date": fmt_settled_time(s.get("settled_time", "")),
        "Traded_Time": "",
        "Last_Updated": "",
        "Deposit_Type": "",
        "Fee_In_Dollars": "",
        "Market_Title": "",
        "Market_Ticker": s.get("ticker", ""),
        "Market_Id": "",
        "Filled": "",
        "Remaining": "",
        "Direction": "",
        "Order_Type": "",
        "Price_In_Cents": "",
        "No_Contracts_Owned": fmt_count(s.get("no_count_fp")),
        "No_Contracts_Average_Price_In_Cents": fmt_avg_price(
            s.get("no_total_cost_dollars"), s.get("no_count_fp")
        ),
        "Yes_Contracts_Owned": fmt_count(s.get("yes_count_fp")),
        "Yes_Contracts_Average_Price_In_Cents": fmt_avg_price(
            s.get("yes_total_cost_dollars"), s.get("yes_count_fp")
        ),
        "Result": s.get("market_result", ""),
        "Profit_In_Dollars": f"{(s.get('revenue') or 0) / 100.0:.2f}",
        "Credit_Reason": "",
        "Credit_Type": "",
        "Introducing_Broker": "",
    }


def fetch_all_settlements(client, page_limit=200):
    settlements = []
    cursor = None
    while True:
        resp = client.get_portfolio_settlements(limit=page_limit, cursor=cursor)
        batch = resp.get("settlements", []) or []
        settlements.extend(batch)
        cursor = resp.get("cursor") or None
        print(f"  fetched {len(batch)} settlements (running total {len(settlements)})")
        if not cursor or not batch:
            break
    return settlements


def main():
    key_id = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
    private_key = load_private_key(
        b64_key=os.environ.get("KALSHI_PRIVATE_KEY", ""),
        file_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt"),
    )

    client = ExchangeClient(
        exchange_api_base="https://api.elections.kalshi.com/trade-api/v2",
        key_id=key_id,
        private_key=private_key,
    )
    print("exchange status:", client.get_exchange_status())

    settlements = fetch_all_settlements(client)
    # API returns newest first; the CSV is also newest first — keep as-is.

    out_path = os.environ.get(
        "SETTLEMENTS_OUT",
        f"Kalshi-Settlements-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv",
    )
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n"
        )
        writer.writeheader()
        for s in settlements:
            writer.writerow(row_from_settlement(s))

    print(f"wrote {len(settlements)} settlements -> {out_path}")


if __name__ == "__main__":
    main()
