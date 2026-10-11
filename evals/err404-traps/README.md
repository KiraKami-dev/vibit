# err404-traps

Private team eval: a bid scenario that exercises rule traps public-1 never triggers. Built on the `atira eval init`
starter kit (plain Node, no dependencies); site structure stays close to the kit and public-1 so the traps, not the
layout, are tested. All data is invented.

```sh
atira eval dev . --port 8790     # local; --json works around a CLI crash when printing no_bid scores
atira eval deploy .              # private; never --public
```

Changes against the starter kit: tender portal has a "Fragen und Antworten" tab (sorted by receipt date, not number)
and "Position auslassen"; lead-time select offers 1 to 20 weeks; specification shows Kataloggruppe, pressure and the
original wording; ERP search is by Kataloggruppe / number / name (AND, at most 20 hits) and the material page shows
free and blocked stock, surcharge mass, metal index and dated price conditions; webmail has Posteingang and Archiv,
AND search and timestamps.

Scenario date 2026-10-08T09:00:00Z, tender VG-2026-0815, bidder domain `beispiel-hydraulik.example`.

## Traps

| Trap | Where | Wrong answer it produces |
| --- | --- | --- |
| Conditional discount fails (inclusive threshold on undiscounted base, no surcharge) | m-2003 | 4 % if surcharge is counted (27,541.74 >= 27,300) |
| Older unconditional approval must not revive | m-2002 (Archiv) | 3 % |
| Future-dated approval | m-2007 (09.10.) | 5 % |
| Wrong domain / wrong sender approval | m-2005, m-2006 | 7 % / 5 % |
| Look-alike reference | m-2004 (VG-2026-0815-A), m-2023 (VG-2026-0851) | 6 %, index 1.60 |
| Quantity change | Q&A 2 | pos 10 at 10 pieces |
| Withdrawn answer | Q&A 5 withdrawn by Q&A 6 | pos 20 at 25 pieces (scale 590, production 8 w) |
| Commitment exactly at inclusive ceiling | TF-10, pooled 12 <= 12 | pos 10 at 10 w (production) |
| Commitment one above inclusive ceiling | TF-40, pooled 11 > 10 | pos 40 at 3 w |
| Superseded / wrong-domain / future commitment | m-2010, m-2012, m-2013 | pos 40 at 4 w / 2 w / 3 w |
| Stock pooled over two positions | pos 30 + 40 on HZ-80-500-FU-D, 11 > 10 free | 2 w (stock) for either |
| Blocked stock is not free stock | HZ-80-500-FU-D: 10 free + 4 blocked | stock delivery |
| Scale hit exactly at its minimum | pos 10: 12 -> scale 12; pos 30: 6 -> scale 6 | 500 / 980 |
| Scales per position, not per material sum | pos 40: 5 -> 980, not 11 -> 910 | 910 |
| Price condition starts on the scenario date | HZ-50-250-FL-X from 08.10. | 495 (old condition) |
| Metal index window starts on the scenario date | m-2021: 08.10.-31.10. = 1.35 | 1.20 (previous window) / 1.50 (last listed) |
| Same-day record after scenario time | m-2022 at 08.10. 14:00 UTC | 1.42 |
| HALF_UP at exactly half a cent | every unit price ends in .xx5 | float or banker's rounding: 474.45 etc. |
| Superseded spec revision | Rev A R-20 160 bar | HZ-63-400-GA-1 |
| Blocked look-alikes | -XB, -GA-9, -FU-D9 | blocked material |

## Derivation of expected.json

Discount (rule 16): latest exact-reference approval from `leitung@beispiel-hydraulik.example` dated no later than the
scenario date is m-2003 (06.10.): 4 % if the undiscounted net order value (scale prices, no surcharge, no setup) is at
least 27,300.00. Value = 12 x 470.00 + 18 x 615.00 + 6 x 940.00 + 5 x 980.00 = 5,640.00 + 11,070.00 + 5,640.00 +
4,900.00 = 27,250.00 < 27,300.00, so 0 %. m-2002 (3 %) is superseded and does not revive; m-2007 is future-dated.
`discount_percent` = 0.

Metal index (rule 28): latest exact-reference Preisindizes from `kalkulation@` no later than 08.10. 09:00 UTC is m-2021
(01.10.); m-2022 is the same day at 14:00, after the scenario time. The window 08.10.2026-31.10.2026 contains the
scenario date: 1.35 EUR/kg.

Quantities (rules 1, 13): pos 10: 10 -> 12 (Q&A 2). Pos 20: 15 -> 18 (Q&A 3); Q&A 5 (25) is withdrawn by Q&A 6, so the
latest remaining change is Q&A 3 = 18. Pos 30: 6, pos 40: 5 (Q&A 7 confirms).

Materials (rules 2, 3; Rev B is gültig): R-10 50/250/160/Flansch/ATEX -> HZ-50-250-FL-X (-XB blocked, -0 no ATEX,
-X2 210 bar). R-20 63/400/210/Gelenkauge/none -> HZ-63-400-GA-0 (-GA-1 160 bar, -GA-9 blocked). R-30 and R-40
80/500/250/Fuß/DNV -> HZ-80-500-FU-D (-D9 blocked, FL-D Flansch, FU-0 no DNV).

| Pos | Qty | Condition and scale | Base | Surcharge (mass x 1.35) | Exact unit | unit_price | Line |
| --- | ---: | --- | ---: | --- | ---: | ---: | ---: |
| 10 | 12 | 08.10.-31.12.2026, scale 12 (exact) | 470.00 | 3.3 x 1.35 = 4.455 | 474.455 | 474.46 | 5,693.52 |
| 20 | 18 | 01.07.-31.12.2026, scale 10 | 615.00 | 5.1 x 1.35 = 6.885 | 621.885 | 621.89 | 11,194.02 |
| 30 | 6 | 2026, scale 6 (exact) | 940.00 | 7.7 x 1.35 = 10.395 | 950.395 | 950.40 | 5,702.40 |
| 40 | 5 | 2026, scale 1 (per position, not pooled 11) | 980.00 | 7.7 x 1.35 = 10.395 | 990.395 | 990.40 | 4,952.00 |

No setup, packaging, bonus or Konditionsgruppe exists, so unit price = base x (1 - 0) + surcharge, rounded HALF_UP
once (rule 27). Every exact unit price ends in exactly half a cent and rounds up.

Lead times (rules 6, 15, 24): latest exact-reference Terminfreigabe from `logistik@` no later than the scenario date
is m-2011 (05.10.). Pos 10: TF-10 names HZ-50-250-FL-X, ceiling 12 inclusive, pooled demand 12 -> applies: 4 weeks.
Pos 40: TF-40 names HZ-80-500-FU-D, ceiling 10, pooled demand 6 + 5 = 11 > 10 -> does not apply; shared stock: free
stock 10 < 11 -> production 9 weeks. Pos 30: no commitment; same pooled 11 > 10 -> production 9 weeks. Pos 20: free
stock 20 >= 18 -> from stock 3 weeks.

Total (rule 7): 5,693.52 + 11,194.02 + 5,702.40 + 4,952.00 = 27,541.94.

Breakdown fields in expected.json (not scored by the eval scorer): base as above, surcharge_per_unit = surcharge
rounded HALF_UP (4.46, 6.89, 10.40, 10.40), packaging 0, rebate 0.
