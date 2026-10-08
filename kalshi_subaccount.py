#!/usr/bin/env python3
"""Kalshi subaccounts -- a helper for the ACCOUNT OWNER to run by hand.

Jack 2026-10-08: "yes subaccount, $250 at risk" -- nfl_snipe_bot.py trades
live in its own numbered subaccount, apart from the IMM's cash and
positions. Creating the subaccount and moving cash into it are the owner's
steps: no bot calls this, and every write asks for a typed confirmation.

    python kalshi_subaccount.py balance 1         # read-only
    python kalshi_subaccount.py create            # a new numbered subaccount
    python kalshi_subaccount.py fund 1 300        # $300 primary -> subaccount 1
    python kalshi_subaccount.py withdraw 1 300    # $300 subaccount 1 -> primary
    python kalshi_subaccount.py transfers         # the transfer history

Kalshi's production host (external-api.kalshi.com), the account key that
kalshi_reads signs with. Transfers move cash on exchange shard 0, where the
NFL props trade. A subaccount's own cash is balance_dollars; the balance's
breakdown is the whole account's.
"""

import argparse
import json
import sys
import uuid
from typing import Any, Callable, Optional

API_BASE = "https://external-api.kalshi.com/trade-api/v2"


def client() -> Any:
    import kalshi_reads as kr
    from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient
    return ExchangeClient(exchange_api_base=API_BASE, key_id=kr.KEY_ID,
                          private_key=kr.load_private_key())


def confirm(prompt: str, word: str, ask: Callable[[str], str] = input) -> bool:
    try:
        return ask(f"{prompt}\nType {word} to go ahead: ").strip() == word
    except EOFError:
        return False


def balance(c: Any, n: int) -> float:
    js = c.get("/portfolio/balance", {"subaccount": n} if n else {})
    return float(js.get("balance_dollars") or 0.0)


def transfer_body(src: int, dst: int, dollars: float) -> dict:
    cents = int(round(dollars * 100))
    if cents <= 0:
        raise ValueError("the amount must be positive")
    if src == dst:
        raise ValueError("from and to are the same subaccount")
    return {"client_transfer_id": str(uuid.uuid4()), "from_subaccount": int(src),
            "to_subaccount": int(dst), "amount_cents": cents, "exchange_index": 0}


def main(argv: Optional[list] = None, c: Any = None,
         ask: Callable[[str], str] = input) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("balance")
    b.add_argument("n", type=int)
    sp.add_parser("create")
    sp.add_parser("transfers")
    for name in ("fund", "withdraw"):
        p = sp.add_parser(name)
        p.add_argument("n", type=int)
        p.add_argument("dollars", type=float)
    args = ap.parse_args(argv)
    c = c or client()

    if args.cmd == "balance":
        print(f"subaccount {args.n}: ${balance(c, args.n):,.2f}")
        return 0
    if args.cmd == "transfers":
        print(json.dumps(c.get("/portfolio/subaccounts/transfers", {}), indent=1))
        return 0
    if args.cmd == "create":
        if not confirm("Create a new numbered Kalshi subaccount on this account?",
                       "CREATE", ask):
            print("not created")
            return 1
        js = c.post(path="/portfolio/subaccounts", body=json.dumps({}))
        print(f"created subaccount {js.get('subaccount_number')} -- fund it with: "
              f"python kalshi_subaccount.py fund {js.get('subaccount_number')} 300")
        return 0
    src, dst = (0, args.n) if args.cmd == "fund" else (args.n, 0)
    body = transfer_body(src, dst, args.dollars)
    if not confirm(f"Move ${args.dollars:,.2f} from subaccount {src} "
                   f"({'primary' if src == 0 else 'numbered'}) to subaccount {dst} "
                   f"({'primary' if dst == 0 else 'numbered'})?", "MOVE", ask):
        print("nothing moved")
        return 1
    # ExchangeClient.post sends the body verbatim: a JSON string, not a dict
    c.post(path="/portfolio/subaccounts/transfer", body=json.dumps(body))
    print(f"moved ${args.dollars:,.2f}; subaccount {args.n} now holds "
          f"${balance(c, args.n):,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
