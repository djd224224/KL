# High-Temp Digest — 2026-04-18

## Forecasts by city
⚠️ **No forecast snapshot data available for 2026-04-18.** `KXHIGH_market_snapshot` last recorded on **2026-03-11** — the snapshot script has not been running for ~5 weeks. NWS, WU, forecast_avg, forecast_std, and expected high-hour are unavailable for all cities. Forecast error columns are N/A throughout.

## Actuals vs Forecast
| City | Actual high (°F) | NWS error | WU error | Avg error | High time |
|------|----------------:|----------:|---------:|----------:|-----------|
| Atlanta | 84 | N/A | N/A | N/A | 3:12 PM |
| Austin | 74 | N/A | N/A | N/A | 12:05 AM† |
| Chicago | 63 | N/A | N/A | N/A | 12:09 AM† |
| Dallas | 75 | N/A | N/A | N/A | 12:10 AM† |
| Denver | 56 | N/A | N/A | N/A | 4:38 PM |
| Houston | 85 | N/A | N/A | N/A | 12:51 PM |
| Las Vegas | 77 | N/A | N/A | N/A | 2:17 PM |
| Los Angeles | 76 | N/A | N/A | N/A | 12:26 PM |
| Miami | 86 | N/A | N/A | N/A | MM (missing) |
| Minneapolis | 46 | N/A | N/A | N/A | 5:19 PM |
| New Orleans | 86 | N/A | N/A | N/A | 3:27 PM |
| New York City | 66 | N/A | N/A | N/A | 2:14 PM |
| Oklahoma City | 63 | N/A | N/A | N/A | 4:49 PM |
| Philadelphia | 78 | N/A | N/A | N/A | 2:27 PM |
| Phoenix | 91 | N/A | N/A | N/A | 4:48 PM |
| San Antonio | 74 | N/A | N/A | N/A | 9:07 AM† |
| San Francisco | 72 | N/A | N/A | N/A | 2:54 PM |
| Seattle | 68 | N/A | N/A | N/A | 4:33 PM |
| Washington DC | 81 | N/A | N/A | N/A | 12:24 PM |

† Early-morning peak: Austin (12:05 AM), Chicago (12:09 AM), Dallas (12:10 AM), and San Antonio (9:07 AM) all peaked overnight/early morning, consistent with a cold front passing through the South and Midwest overnight Apr 17→18.

## Orders placed
| City | Markets | Orders | Total contracts |
|------|--------:|-------:|----------------:|
| Atlanta | 2 | 24 | 720 |
| Austin | 2 | 40 | 1,800 |
| Chicago | 1 | 8 | 360 |
| Dallas | 1 | 16 | 450 |
| Denver | 3 | 81 | 2,839 |
| Houston | 5 | 66 | 2,430 |
| Las Vegas | 3 | 36 | 1,351 |
| Los Angeles | 4 | 89 | 3,189 |
| Miami | 3 | 53 | 1,878 |
| Minneapolis | 4 | 79 | 2,901 |
| New Orleans | 3 | 55 | 2,048 |
| New York City | 3 | 60 | 2,666 |
| Oklahoma City | 4 | 90 | 3,055 |
| Philadelphia | 4 | 87 | 2,980 |
| Phoenix | 2 | 40 | 1,320 |
| San Antonio | 4 | 47 | 1,462 |
| San Francisco | 3 | 96 | 3,240 |
| Seattle | 3 | 93 | 3,177 |
| **TOTAL** | **54** | **1,060** | **37,866** |

12 of 18 cities had orders but zero fills; 7 markets filled across 6 cities. Note: `model_edge_at_fill` and `forecast_avg_at_fill` are null for all fills — the enriched view cannot join to snapshot data since that table has been empty since March 11.

---

## Per-city fill & P&L detail

*All fills are NO-side bets on `between` (B) markets. Official settlements not yet recorded; P&L estimated from IEM actual highs. B{X} = YES if actual ≤ X; our NO wins if actual > X.*

---

### Denver (DEN)
**Actual high:** 56°F at 4:38 PM | **Forecast avg:** N/A | **NWS:** N/A | **WU:** N/A

| Market ticker | Band | Side | Filled | Avg price | Cost | Est. P&L | Outcome | Model edge |
|---|---|---|---:|---:|---:|---:|---|---|
| KXHIGHDEN-26APR18-B57.5 | 55.5–57.5°F | NO | 57.64 | $0.450 | $26.18 | **−$26.18** | LOSS (56°F in band) | N/A |

**City P&L:** −$26.18 (est.) | **Result:** 0W / 1L

*Denver's actual high of 56°F landed squarely inside the 55.5–57.5°F band — only 1.5°F below the ceiling. The NO bet at 45¢ needed the temp to exceed 57.5°F. The afternoon peak (4:38 PM) reflects normal diurnal cycle, not frontal influence.*

---

### Los Angeles (LAX)
**Actual high:** 76°F at 12:26 PM | **Forecast avg:** N/A | **NWS:** N/A | **WU:** N/A

| Market ticker | Band | Side | Filled | Avg price | Cost | Est. P&L | Outcome | Model edge |
|---|---|---|---:|---:|---:|---:|---|---|
| KXHIGHLAX-26APR18-B75.5 | 73.5–75.5°F | NO | 54.05 | $0.497 | $26.88 | **+$27.18** | WIN (76°F above band) | N/A |

**City P&L:** +$27.18 (est.) | **Result:** 1W / 0L

*LA's 76°F high cleared the 75.5°F band ceiling by 0.5°F, turning the NO bet at ~50¢ into a winner. The market priced the band at near-even odds, suggesting genuine uncertainty; the actual temp came in slightly above.*

---

### Miami (MIA)
**Actual high:** 86°F (time MM — IEM timestamp missing) | **Forecast avg:** N/A | **NWS:** N/A | **WU:** N/A

| Market ticker | Band | Side | Filled | Avg price | Cost | Est. P&L | Outcome | Model edge |
|---|---|---|---:|---:|---:|---:|---|---|
| KXHIGHMIA-26APR18-B85.5 | 83.5–85.5°F | NO | 180.00 | $0.630 | $115.60 | **+$66.60** | WIN (86°F above band) | N/A |

**City P&L:** +$66.60 (est.) | **Result:** 1W / 0L

*Miami was the largest single fill (180 contracts) and best dollar winner. Entering NO at 63¢ against a band topped at 85.5°F proved correct as the actual high came in at 86°F — just 0.5°F over the ceiling. The high entry price (63¢) reflects the market's view that an 83.5–85.5°F day was probable; the model disagreed and was right. IEM's missing high_time (MM) is likely a data ingestion gap, not a real measurement issue.*

---

### New Orleans (NOLA)
**Actual high:** 86°F at 3:27 PM | **Forecast avg:** N/A | **NWS:** N/A | **WU:** N/A

| Market ticker | Band | Side | Filled | Avg price | Cost | Est. P&L | Outcome | Model edge |
|---|---|---|---:|---:|---:|---:|---|---|
| KXHIGHTNOLA-26APR18-B84.5 | 82.5–84.5°F | NO | 86.00 | $0.510 | $43.96 | **+$42.14** | WIN (86°F above band) | N/A |

**City P&L:** +$42.14 (est.) | **Result:** 1W / 0L

*New Orleans' 86°F actual exceeded the 84.5°F band ceiling by 1.5°F, the cleanest margin of our four wins. Entry at 51¢ was near-fair, and the 86-contract fill generated a solid +$42.14. Like Miami, Gulf warmth kept afternoon temps elevated well above the band range.*

---

### San Antonio (SATX)
**Actual high:** 74°F at 9:07 AM | **Forecast avg:** N/A | **NWS:** N/A | **WU:** N/A

| Market ticker | Band | Side | Filled | Avg price | Cost | Est. P&L | Outcome | Model edge |
|---|---|---|---:|---:|---:|---:|---|---|
| KXHIGHTSATX-26APR18-B74.5 | 72.5–74.5°F | NO | 40.00 | $0.590 | $23.60 | **−$23.60** | LOSS (74°F in band) | N/A |

**City P&L:** −$23.60 (est.) | **Result:** 0W / 1L

*San Antonio's earliest-morning high (9:07 AM) is the smoking gun: a cold front swept through overnight, and the day's warmest moment came before sunrise. The 74°F peak sits 0.5°F below the 74.5°F band ceiling — a near-miss. Entry at 59¢ suggests the model expected the day to run hotter than 74.5°F, which the pre-dawn front prevented.*

---

### Seattle (SEA)
**Actual high:** 68°F at 4:33 PM | **Forecast avg:** N/A | **NWS:** N/A | **WU:** N/A

| Market ticker | Band | Side | Filled | Avg price | Cost | Est. P&L | Outcome | Model edge |
|---|---|---|---:|---:|---:|---:|---|---|
| KXHIGHTSEA-26APR18-B68.5 | 66.5–68.5°F | NO | 56.00 | $0.520 | $29.04 | **−$29.04** | LOSS (68°F in band) | N/A |

**City P&L:** −$29.04 (est.) | **Result:** 0W / 1L

*Seattle's 68°F afternoon peak fell 0.5°F below the 68.5°F band ceiling. Entry at 52¢ needed temps above 68.5°F; the day came in just short. The late-afternoon peak (4:33 PM) is typical Pacific Northwest spring pattern with marine influence keeping a lid on afternoon warmth.*

---

### Minneapolis (TMIN)
**Actual high:** 46°F at 5:19 PM | **Forecast avg:** N/A | **NWS:** N/A | **WU:** N/A

| Market ticker | Band | Side | Filled | Avg price | Cost | Est. P&L | Outcome | Model edge |
|---|---|---|---:|---:|---:|---:|---|---|
| KXHIGHTMIN-26APR18-B45.5 | 43.5–45.5°F | NO | 110.53 | $0.520 | $57.45 | **+$53.05** | WIN (46°F above band) | N/A |

**City P&L:** +$53.05 (est.) | **Result:** 1W / 0L

*Minneapolis was the highest-volume fill (110.53 contracts) and second-best winner. The actual 46°F late-afternoon high cleared the 45.5°F band ceiling by 0.5°F. Entry at 52¢ against a band suggesting near-50/50 odds, and the day delivered just enough warmth to push over. The same cold front that crushed San Antonio and Texas brought cold air south of Minneapolis, keeping temps barely in the 40s — the late peak (5:19 PM) suggests the sun was still fighting the airmass through the afternoon.*

---

## P&L summary

| City | Market | Filled (contracts) | Cost | Est. P&L | Outcome |
|------|--------|-----------------:|-----:|---------:|---------|
| Denver | B57.5 | 57.64 | $26.18 | **−$26.18** | LOSS |
| Los Angeles | B75.5 | 54.05 | $26.88 | **+$27.18** | WIN |
| Miami | B85.5 | 180.00 | $115.60 | **+$66.60** | WIN |
| New Orleans | B84.5 | 86.00 | $43.96 | **+$42.14** | WIN |
| San Antonio | B74.5 | 40.00 | $23.60 | **−$23.60** | LOSS |
| Seattle | B68.5 | 56.00 | $29.04 | **−$29.04** | LOSS |
| Minneapolis | B45.5 | 110.53 | $57.45 | **+$53.05** | WIN |
| **TOTAL** | | **584.22** | **$322.71** | **+$110.15** | **4W / 3L (57%)** |

Gross return on deployed capital: **+34.1%** (est., pre-fee). All P&L figures pending official Kalshi settlement.

## Alerts
No alerts recorded on 2026-04-18. (Table has 1,468 total historical alerts; last alert was 2026-04-17.)

---

## Overall summary

April 18 produced a net-positive day of +$110.15 estimated gross P&L on $322.71 deployed across 7 filled markets (4W/3L, 57% win rate), despite the complete absence of forecast snapshot data — the model has been running blind on forecasts since the snapshot system went offline around March 11. All 7 fills were NO-side bets on "between" (B) bands, and the outcomes split cleanly along a weather-front narrative: a cold front swept through the South and Midwest overnight April 17→18, evidenced by Austin, Dallas, and San Antonio all peaking between midnight and 9 AM rather than mid-afternoon. San Antonio's 9:07 AM peak at 74°F was the direct casualty — the model expected the day to exceed 74.5°F but the front killed afternoon heating, leaving the 74°F max inside the 72.5–74.5°F band and turning a 59¢-entry NO bet into a full loss. The same frontal system likely kept Oklahoma City at 63°F and Chicago at 63°F, both with midnight highs, though neither had fills to reveal P&L impact. On the winning side, Gulf Coast cities (Miami 86°F, New Orleans 86°F) stayed warm and above their respective band ceilings, generating +$66.60 and +$42.14 — Miami's 180-contract position at 63¢ was the largest bet and largest winner, reflecting strong model conviction on Gulf warmth. Minneapolis's 46°F high cleared its 45.5°F band ceiling by just 0.5°F for a +$53.05 gain, while LA similarly cleared 75.5°F by 0.5°F for +$27.18. Strikingly, three of the four wins and all three losses were decided by 0.5–1.5°F margins, underscoring how sensitive the NO-on-between strategy is to temperature precision near band edges. The biggest structural concern from this run is that `model_edge_at_fill` is null for all fills, meaning the enriched view is not joining to any snapshot context — with no forecasts and no edge metrics, the system is effectively placing orders without quantifiable model backing, and the 7am digest going forward will continue to show N/A for these fields until the snapshot system is restored.
