# High-Temp Digest — 2026-04-18

## Forecasts by city
⚠️ **No forecast snapshot data available.** `KXHIGH_market_snapshot` last recorded on 2026-03-11. The snapshot script has not been running since early March 2026. NWS/WU forecasts, forecast_avg, and expected high-hour cannot be reported for this date.

## Actuals
*Source: IEM CLI readings (KXHIGH_cli_readings). Settlement not yet official; these may differ slightly from Kalshi's official settlement temperatures.*

| City | Actual high (°F) | High time | NWS error | WU error | Avg error |
|------|----------------:|-----------|----------:|---------:|----------:|
| Atlanta | 84 | 3:12 PM | N/A | N/A | N/A |
| Austin | 74 | 12:05 AM | N/A | N/A | N/A |
| Chicago | 63 | 12:09 AM | N/A | N/A | N/A |
| Dallas | 75 | 12:10 AM | N/A | N/A | N/A |
| Denver | 56 | 4:38 PM | N/A | N/A | N/A |
| Houston | 85 | 12:51 PM | N/A | N/A | N/A |
| Las Vegas | 77 | 2:17 PM | N/A | N/A | N/A |
| Los Angeles | 76 | 12:26 PM | N/A | N/A | N/A |
| Miami | 86 | (MM) | N/A | N/A | N/A |
| Minneapolis | 46 | 5:19 PM | N/A | N/A | N/A |
| New Orleans | 86 | 3:27 PM | N/A | N/A | N/A |
| New York City | 66 | 2:14 PM | N/A | N/A | N/A |
| Oklahoma City | 63 | 4:49 PM | N/A | N/A | N/A |
| Philadelphia | 78 | 2:27 PM | N/A | N/A | N/A |
| Phoenix | 91 | 4:48 PM | N/A | N/A | N/A |
| San Antonio | 74 | 9:07 AM | N/A | N/A | N/A |
| San Francisco | 72 | 2:54 PM | N/A | N/A | N/A |
| Seattle | 68 | 4:33 PM | N/A | N/A | N/A |
| Washington DC | 81 | 12:24 PM | N/A | N/A | N/A |

*Errors are N/A — forecast snapshot data unavailable.*

## Trading
*Orders placed from `KXHIGH_orders` (forecast_date = 2026-04-18). Fills from `KXHIGH_fills_enriched` (event_date = 2026-04-18). Settlements not yet recorded; P&L is estimated from IEM actual highs using band resolution logic (B{X} = YES if actual high falls in (X−2, X] band; T{X} = YES if actual high > upper B-band).*

| City | Orders placed | Contracts ordered | Fills (contracts) | Est. P&L | Notes |
|------|-------------:|------------------:|------------------:|---------:|-------|
| Atlanta | 24 | 720 | 0 | — | No fills |
| Austin | 40 | 1,800 | 0 | — | No fills |
| Chicago | 8 | 360 | 0 | — | No fills |
| Dallas | 16 | 450 | 0 | — | No fills |
| Denver | 81 | 2,839 | 57.64 | **−$25.94** | B57.5 filled; 56°F in band → loss |
| Houston | 66 | 2,430 | 0 | — | No fills |
| Las Vegas | 36 | 1,351 | 0 | — | No fills |
| Los Angeles | 89 | 3,189 | 54.05 | **+$27.17** | B75.5 filled; 76°F above band → win |
| Miami | 53 | 1,878 | 180.00 | **+$66.60** | B85.5 filled; 86°F above band → win |
| Minneapolis | 79 | 2,901 | 110.53 | **+$53.05** | B45.5 filled; 46°F above band → win |
| New Orleans | 55 | 2,048 | 86.00 | **+$42.14** | B84.5 filled; 86°F above band → win |
| New York City | 60 | 2,666 | 0 | — | No fills |
| Oklahoma City | 90 | 3,055 | 0 | — | No fills |
| Philadelphia | 87 | 2,980 | 0 | — | No fills |
| Phoenix | 40 | 1,320 | 0 | — | No fills |
| San Antonio | 47 | 1,462 | 40.00 | **−$23.60** | B74.5 filled; 74°F in band → loss |
| San Francisco | 96 | 3,240 | 0 | — | No fills |
| Seattle | 93 | 3,177 | 56.00 | **−$29.12** | B68.5 filled; 68°F in band → loss |
| **TOTAL** | **1,060** | **37,866** | **584.22** | **+$110.30** | 4W / 3L |

## P&L by temperature band

*All fills were NO-side bets. Estimated P&L based on IEM actual high temps; official settlement pending.*

| Market ticker | City | Band (°F) | Actual high | Side | Fills | Avg entry | Total cost | Est. P&L | Est. outcome |
|--------------|------|----------:|------------:|------|------:|----------:|-----------:|---------:|-------------|
| KXHIGHDEN-26APR18-B57.5 | Denver | 55.5–57.5 | 56 | NO | 57.64 | $0.45 | $26.18 | −$25.94 | LOSS (high in band) |
| KXHIGHLAX-26APR18-B75.5 | Los Angeles | 73.5–75.5 | 76 | NO | 54.05 | $0.497 | $26.88 | +$27.17 | WIN (high above band) |
| KXHIGHMIA-26APR18-B85.5 | Miami | 83.5–85.5 | 86 | NO | 180.00 | $0.630 | $115.60 | +$66.60 | WIN (high above band) |
| KXHIGHTNOLA-26APR18-B84.5 | New Orleans | 82.5–84.5 | 86 | NO | 86.00 | $0.510 | $43.96 | +$42.14 | WIN (high above band) |
| KXHIGHTSATX-26APR18-B74.5 | San Antonio | 72.5–74.5 | 74 | NO | 40.00 | $0.590 | $23.60 | −$23.60 | LOSS (high in band) |
| KXHIGHTSEA-26APR18-B68.5 | Seattle | 66.5–68.5 | 68 | NO | 56.00 | $0.520 | $29.04 | −$29.12 | LOSS (high in band) |
| KXHIGHTMIN-26APR18-B45.5 | Minneapolis | 43.5–45.5 | 46 | NO | 110.53 | $0.520 | $57.45 | +$53.05 | WIN (high above band) |

**Summary:** 4 wins / 3 losses · Total contracts filled: 584.22 · Total cost deployed: $322.71 · **Est. gross P&L: +$110.30** (est. return: +34.2%)

## Alerts
- No alerts recorded on 2026-04-18. (Latest alert in `KXHIGH_alerts` is from 2026-04-17; 1,468 total alerts on record.)

## Overall summary
April 18 was a productive day despite the absence of forecast-snapshot data, with 1,060 orders placed across 18 cities and 54 markets, of which 7 markets actually filled — all on the NO side — representing 584 contracts and $322.71 deployed. The day was net positive by an estimated +$110.30 (+34.2% on deployed capital), with 4 wins and 3 losses. The two biggest wins were Miami (+$66.60) and New Orleans (+$42.14), where warm Gulf-influenced weather pushed actual highs (86°F in both cities) clearly above their respective upper band edges (85.5°F and 84.5°F), vindicating the NO strategy at entry prices of 63¢ and 51¢. Minneapolis was a clean model-edge win: the 46°F actual high fell just above the 43.5–45.5°F band, and the 110-contract fill at 52¢ generated +$53. The three losses came from bands that the temperature landed inside: Denver's 56°F settled squarely in the 55.5–57.5°F band (−$25.94), San Antonio's 74°F in 72.5–74.5°F (−$23.60), and Seattle's 68°F in 66.5–68.5°F (−$29.12) — notably, all three losses involved the actual high landing only 0.5–1°F below the band ceiling, suggesting the model may be systematically underpricing the risk of temps landing at the high end of a band. All settlements for 2026-04-18 markets are still pending; realized P&L will differ slightly from estimates due to Kalshi's official station readings and fee adjustments.
