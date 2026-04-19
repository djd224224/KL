# High-Temp Digest — 2026-04-18

## Forecasts
⚠️ No snapshot data — `KXHIGH_market_snapshot` last recorded **2026-03-11**. NWS/WU/forecast_avg unavailable; all forecast error columns are N/A.

## Fills & P&L

*All positions are NO-side on `between` (B) markets. Band resolution: `B{X}` = YES if actual temp ∈ {floor(X), floor(X)+1} — the two integers straddling X. Our NO bet wins only if actual falls outside that 2°F window. Settlements not yet official (last settled date in BQ: 2026-04-17); outcomes estimated from IEM cli_readings. Verified against Apr 17 official settlements.*

| City | Market | Band | Actual | Filled | Avg price | Cost | Est. P&L | Outcome |
|------|--------|------|-------:|-------:|----------:|-----:|---------:|---------|
| Denver | KXHIGHDEN-26APR18-B57.5 | 57–58°F | **56°F** | 57.64 | $0.454 | $26.18 | **+$31.46** | WIN — below band |
| New Orleans | KXHIGHTNOLA-26APR18-B84.5 | 84–85°F | **86°F** | 86.00 | $0.511 | $43.96 | **+$42.04** | WIN — above band |
| Los Angeles | KXHIGHLAX-26APR18-B75.5 | 75–76°F | **76°F** | 54.05 | $0.497 | $26.88 | **−$26.88** | LOSS — in band |
| Miami | KXHIGHMIA-26APR18-B85.5 | 85–86°F | **86°F** | 180.00 | $0.642 | $115.60 | **−$115.60** | LOSS — in band |
| San Antonio | KXHIGHTSATX-26APR18-B74.5 | 74–75°F | **74°F** | 40.00 | $0.590 | $23.60 | **−$23.60** | LOSS — in band |
| Seattle | KXHIGHTSEA-26APR18-B68.5 | 68–69°F | **68°F** | 56.00 | $0.519 | $29.04 | **−$29.04** | LOSS — in band |
| Minneapolis | KXHIGHTMIN-26APR18-B45.5 | 45–46°F | **46°F** | 110.53 | $0.520 | $57.45 | **−$57.45** | LOSS — in band |
| **TOTAL** | | | | **584.22** | | **$322.71** | **−$179.07** | **2W / 5L (29%)** |

**Return on deployed capital: −55.5% (est.)**

## Actuals (all 19 cities)

| City | Actual high | High time | Orders placed | Contracts | Filled? |
|------|------------:|-----------|---------------|-----------|---------|
| Atlanta | 84°F | 3:12 PM | B89.5, T92 | 720 | No |
| Austin | 74°F | 12:05 AM† | B75.5, T78 | 1,800 | No |
| Chicago | 63°F | 12:09 AM† | T72 | 360 | No |
| Dallas | 75°F | 12:10 AM† | T80 | 450 | No |
| **Denver** | **56°F** | 4:38 PM | B55.5, B57.5, T62 | 2,839 | **B57.5 ✓** |
| Houston | 85°F | 12:51 PM | B79.5–T86 (5 markets) | 2,430 | No |
| Las Vegas | 77°F | 2:17 PM | B78.5, B80.5, T83 | 1,351 | No |
| **Los Angeles** | **76°F** | 12:26 PM | B73.5, B75.5, B77.5, T80 | 3,189 | **B75.5 ✓** |
| **Miami** | **86°F** | — | B85.5, B87.5, T88 | 1,878 | **B85.5 ✓** |
| **Minneapolis** | **46°F** | 5:19 PM | B43.5, B45.5, B47.5, T50 | 2,901 | **B45.5 ✓** |
| **New Orleans** | **86°F** | 3:27 PM | B84.5, B86.5, T89 | 2,048 | **B84.5 ✓** |
| New York City | 66°F | 2:14 PM | B65.5, B67.5, T68 | 2,666 | No |
| Oklahoma City | 63°F | 4:49 PM | B62.5, B64.5, B66.5, T69 | 3,055 | No |
| Philadelphia | 78°F | 2:27 PM | B70.5, B72.5, B74.5, T77 | 2,980 | No |
| Phoenix | 91°F | 4:48 PM | B93.5, T96 | 1,320 | No |
| **San Antonio** | **74°F** | 9:07 AM† | B72.5, B74.5, B76.5, T79 | 1,462 | **B74.5 ✓** |
| San Francisco | 72°F | 2:54 PM | B70.5, B72.5, T75 | 3,240 | No |
| **Seattle** | **68°F** | 4:33 PM | B66.5, B68.5, T71 | 3,177 | **B68.5 ✓** |
| Washington DC | 81°F | 12:24 PM | — | — | No |

† Early-morning high — front passed overnight Apr 17→18. Austin, Dallas, Chicago, San Antonio all peaked between midnight and 9 AM.

## Alerts
No alerts on 2026-04-18.

## Overall summary

April 18 was a bad day: −$179.07 on $322.71 deployed (−55.5%), with 5 losses and only 2 wins. The fundamental problem was that five of the seven filled bands were hit almost dead-on by the actual temperature — the NO strategy bets that the temp falls *outside* a 2°F window, so near-miss accuracy is actually the worst-case scenario. Miami was the largest loss (−$115.60) because 180 contracts were filled on B85.5, and the actual 86°F landed squarely in the {85, 86}°F band; LA (−$26.88) and Minneapolis (−$57.45) had the same story with 76°F in the {75, 76}°F band and 46°F in the {45, 46}°F band respectively. A cold front swept through the South overnight Apr 17→18 — Austin, Dallas, Chicago, and San Antonio all peaked before 12:10 AM — which compressed the daytime range in those cities (no fills, so no direct damage), but did not save the positions that were already entered. San Antonio's 9:07 AM peak at 74°F was a direct casualty of the front: the day's warmest moment came before sunrise, and the 74°F peak sat in the {74, 75}°F band, losing the NO bet at 59¢ despite the front otherwise bringing cooling. The two winners — Denver (+$31.46) and New Orleans (+$42.04) — both cleared their bands cleanly: Denver's 56°F was one degree below the {57, 58}°F band, and New Orleans's 86°F was one degree above the {84, 85}°F band. The critical data gap is that `model_edge_at_fill` is null for all positions (snapshot system down since March 11), meaning there's no visibility into whether these entries had positive expected value at time of placement — the session is effectively flying blind on model edge.
