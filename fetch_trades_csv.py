# -*- coding: utf-8 -*-
"""Pull portfolio fills from the Kalshi API and write a Trade CSV that matches
the format expected by the kalshi-dashboard skill's `analyze.py`.

Uses the same auth pattern as fetch_settlements_csv.py (env vars
KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY / KALSHI_PRIVATE_KEY_PATH, falling
back to Lisa_Kalshi.txt in the working directory).
"""

import os
import csv
import base64
from datetime import datetime

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


def row_from_fill(f):
    side = (f.get("side") or "").lower()
    yes_px = float(f.get("yes_price_dollars") or 0)
    no_px = float(f.get("no_price_dollars") or 0)
    # Price_In_Cents is the price of the side actually traded, as an integer cent.
    px_dollars = yes_px if side == "yes" else no_px
    price_cents = int(round(px_dollars * 100))
    count = int(float(f.get("count_fp") or 0))
    fee = float(f.get("fee_cost") or 0)
    is_taker = bool(f.get("is_taker"))
    return {
        "type": "Trade",
        "Status": "",
        "Amount_In_Dollars": str(count),  # misnamed; Kalshi CSV puts contract count here
        "Original_Date": f.get("created_time", ""),
        "Traded_Time": "",
        "Last_Updated": "",
        "Deposit_Type": "",
        "Fee_In_Dollars": f"{fee:.2f}",
        "Market_Title": "",
        "Market_Ticker": f.get("market_ticker") or f.get("ticker", ""),
        "Market_Id": "",
        "Filled": "",
        "Remaining": "",
        "Direction": "Yes" if side == "yes" else ("No" if side == "no" else ""),
        "Order_Type": "Taker" if is_taker else "Maker",
        "Price_In_Cents": str(price_cents),
        "No_Contracts_Owned": "",
        "No_Contracts_Average_Price_In_Cents": "",
        "Yes_Contracts_Owned": "",
        "Yes_Contracts_Average_Price_In_Cents": "",
        "Result": "",
        "Profit_In_Dollars": "",
        "Credit_Reason": "",
        "Credit_Type": "",
        "Introducing_Broker": "",
    }


def fetch_all_fills(client, page_limit=200):
    fills = []
    cursor = None
    page = 0
    while True:
        resp = client.get_fills(limit=page_limit, cursor=cursor)
        batch = resp.get("fills", []) or []
        fills.extend(batch)
        cursor = resp.get("cursor") or None
        page += 1
        if page % 10 == 0 or not cursor or not batch:
            print(f"  fetched {len(batch)} fills (running total {len(fills)})")
        if not cursor or not batch:
            break
    return fills


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
    fills = fetch_all_fills(client)

    out_path = os.environ.get(
        "TRADES_OUT",
        f"Kalshi-Trades-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv",
    )
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n"
        )
        writer.writeheader()
        for fill in fills:
            writer.writerow(row_from_fill(fill))

    print(f"wrote {len(fills)} fills -> {out_path}")


if __name__ == "__main__":
    main()
