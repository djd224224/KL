2m 11s
Run python ncaab_order_script_v42.py
Traceback (most recent call last):
  File "/home/runner/work/KL/KL/ncaab_order_script_v42.py", line 1574, in <module>
Testing connection...
    df_orders, df_market_snapshot_signals, position_ledger = place_orders_from_df(df_results)
✓ Connected! Trading active: True

======================================================================
FETCHING DATA FOR KXNCAABMENTION
======================================================================

Fetching ALL markets...
  Fetching markets page 1...
    Retrieved 200 markets (total: 200)
  Fetching markets page 2...
    Retrieved 200 markets (total: 400)
  Fetching markets page 3...
    Retrieved 200 markets (total: 600)
  Fetching markets page 4...
    Retrieved 200 markets (total: 800)
  Fetching markets page 5...
    Retrieved 200 markets (total: 1000)
  Fetching markets page 6...
    Retrieved 200 markets (total: 1200)
  Fetching markets page 7...
    Retrieved 200 markets (total: 1400)
  Fetching markets page 8...
    Retrieved 200 markets (total: 1600)

✓ Fetched 1600 total markets

Built df_results: (1600, 14)

Market status breakdown:
  finalized: 1407
  active: 128
  closed: 65

  Active/tradeable markets: 128
  Settled markets with results: 1407

Building rolling summary (from SETTLED markets only)...
  Using 1407 settled markets with results
  df_summary: (65, 5)

Building team-level rolling summary...
  df_summary_rolling_by_team: (2678, 8)

Dataframes created:
  df_results: (1600, 17)
  df_summary: (65, 5)
  df_summary_rolling: (606, 7)
  df_summary_rolling_by_team: (2678, 8)

✓ Data collection complete!
Testing connection...
✓ Connected! Trading active: True

======================================================================
CONFIGURATION — v4.2-NCAAB
======================================================================
Run ID: f10d98f5-ef52-4435-b8b2-caaa77c12326
Series: KXNCAABMENTION
Pricing: hybrid | Bayesian K=25
Position caps: market=150 [was300] event=800 [was2000]
  Moderate at 75 [was100], stop at 150 [was300]
Pairing: threshold=60 aggressive=100
Price filters:
  YES: 20-75c floor=20c (Kelly gate replaces prob_floor)
  NO:  15-75c sweet=65-75c @1.5x
  EV gate: 2%
BLOCKED: []
SAFE BUNDLE (≥1.5x No): ['SCHE', 'NIL', 'ELBO', 'MARC']
SECONDARY: ['DOUB', 'TRAN', 'WALK', 'BUZZ', 'ALLA', 'OVER', 'RECR', 'DRAF', 'ANKL', 'AIRB', 'ALLE', 'RECO', 'ALL']
Team overrides: 16 combos
Base contracts: [10, 10, 10, 12, 12]... (was 15/15/15/20/20)
======================================================================


Validating df_results...
Active markets: 128

======================================================================
CANCELING ORDERS FOR KXNCAABMENTION
======================================================================
Found 710 resting KXNCAABMENTION orders to cancel
✓ Canceled 710/710 orders


======================================================================
CHECKING EXISTING POSITIONS FOR KXNCAABMENTION
======================================================================
  No non-zero positions found


======================================================================
BUILDING FILLS-BASED POSITION LEDGER
======================================================================
  Fetching fills from *** API for KXNCAABMENTION...
  ✓ Loaded 1857 fills across 432 markets from API
  Mapped 'count_fp' -> 'contracts'
  Mapped 'yes_price_dollars' -> 'yes_price'
  ✓ Built ledger for 432 markets

======================================================================
POSITION LEDGER SUMMARY
======================================================================
  Markets: 432 | Paired: 1426 | Net: 18831
  Paired ratio: 7.0%
  Guaranteed paired profit: $111.12
  Worst-case total P&L: $-8579.82

  TOP 5 BY PAIRED PROFIT:
    KXNCAABMENTION-26MAR18MOHSMU-RECO             paired=78 edge=9.3¢ profit=$7.23
    KXNCAABMENTION-26FEB17UNCNCST-DOUB            paired=50 edge=14.0¢ profit=$7.00
    KXNCAABMENTION-26FEB16HOUISU-OVER             paired=50 edge=13.0¢ profit=$6.50
    KXNCAABMENTION-26FEB17TTUASU-ALL              paired=75 edge=6.5¢ profit=$4.88
    KXNCAABMENTION-26FEB26MSUPUR-DOUB             paired=30 edge=15.2¢ profit=$4.57
======================================================================


KXNCAABMENTION-26MAR21SLUMICH-WALK:
    Time: 26.8h (0.90x) | OI: 0 (0.80x)
    Fair: YES=17¢ NO=83¢ | Mults: YES=0.07x NO=0.22x
    → NO:  20 contracts
                                                             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/runner/work/KL/KL/ncaab_order_script_v42.py", line 1533, in place_orders_from_df
    orders = build_order_objects_for_market(row.to_dict(), existing_positions,
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/runner/work/KL/KL/ncaab_order_script_v42.py", line 1442, in build_order_objects_for_market
    return order
           ^^^^^
NameError: name 'order' is not defined. Did you mean: 'orders'?
