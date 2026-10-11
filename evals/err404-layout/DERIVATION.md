# How expected.json follows from world.js

All data is invented. Scenario date 2026-11-16T09:00:00Z, tender TR-2026-0815, bidder mailbox domain
`fluidtech.example`. `python3 derive.py` recomputes the answer in Decimal (without the agent's calc.py) and checks it
against expected.json.

## Governing records (rule 21: latest, exact reference, right sender, not after the scenario date)

| Key | Record used | Ignored |
| --- | --- | --- |
| Spec revision | TS-4471 Rev. C (status Current, in the revision history) | Rev. A (shown first), Rev. B |
| Discount | m-202 leitung@, 05 Nov: 4.5 % if undiscounted order value >= 56,400.00 | m-201 3 % (older), m-203 (ref -B), m-204 7 % (20 Nov, future), m-205 (controlling@) |
| FX | m-212 finanzen@ Devisenfixing, 09 Nov: FX-USD-11 1 EUR = 1.2800 USD | m-211 (1.31, older), m-213 (ref 0851), FX-USD-10 (key of an expired condition) |
| Metal index | m-222 kalkulation@ Preisindizes, 03 Nov, Nov window: MX-ST1 1,310.00 EUR/t, MX-ST2 980.00 EUR/t | Oct/Dec windows, m-221 |
| Konditionsregeln | m-232 kalkulation@, 06 Nov: KG-B = base + mass surcharge; KG-N = no discount | m-231 (KG-B base only) |
| Ruestpools | m-242 kalkulation@, 06 Nov: SP-NORTH 1,234.57 floor + largest remainder; SP-SOUTH 777.77 half up, rest to largest weight | m-241 draft |
| Projektbonus | m-251 finanzen@ PB-7: 0 % to 10,000; 1.5 % 10,000 to 20,000; 2.5 % above | m-252 (ref -B) |
| Verpackungstarife | m-262 logistik@, 02 Nov: PK-S 14.50, PK-L 9.80, PK-H 38.00 per carton | m-261 |
| Terminfreigabe | m-272 logistik@, 08 Nov: DC-2 FT-6340-FA2 4 wk ceiling 30; DC-5 FT-8050-TD2 3 wk ceiling 20 | m-271 (DC-2 5 wk) |
| Technikfreigabe | m-282 technik@ Rev. C: ER-60 10 mm per end | m-281 (Rev. B, 15 mm) |

Clarifications (buyer extranet, not the sourcing hub): Q-02 groups (10: KG-B, 40: KG-N), Q-03 tariffs (10, 20: PK-S;
30, 50: PK-L; 40, 60: PK-H), Q-04 position 30 = 6 sets, Q-05 amendment to Rev. B (ignored), Q-06 commitments (20: DC-2,
40: DC-5), Q-07 position 50 = 40 pcs (withdrawn by Q-10), Q-08 setup fees (40: 360.00, 60: 250.00) and pools
(SP-NORTH: 10, 20, 50; SP-SOUTH: 30, 40, 60), Q-09 amendment Rev. C R-40 pressure 250 bar, Q-11 bonus members 30, 50, 60.

## Positions: quantity, material, lead time

| Pos | Quantity | Requirement (Rev. C + amendments) | Material | Lead time |
| --- | --- | --- | --- | --- |
| 10 | 18 pcs | 63/400, 210 bar, Flange, ATEX | FT-6340-FA2 (FA1 blocked, F00 no ATEX, FA5 250 bar) | pooled 10+20 = 30 > free stock 25 (blocked 20 not counted): production 9 wk |
| 20 | 12 pcs | same as R-10 | FT-6340-FA2 | DC-2 matches material, pooled 30 <= 30: 4 wk |
| 30 | 6 sets x 4 = 24 pcs (Rev. C: 1 set = 4; Rev. A/B said 5) | 40/150, 160, Clevis, none | FT-4015-C00 | 24 <= 30: stock 3 wk |
| 40 | 8 pcs | 80/500, 250 bar (Q-09), Trunnion, DNV | FT-8050-TD5 (TD2 = 210 bar, FD5 = Foot) | 8 > free 5: production 40 working days = 8 wk; DC-5 names TD2, does not apply |
| 50 | 30 pcs (Q-07 withdrawn) | 50/300, 160, Rear eye, none | FT-5030-E00 (E01 blocked, 5025 = Rev. B stroke) | 30 <= 40: stock 1 wk |
| 60 | 10 pcs | usable 380 + 2 x 10 = nominal 400, 250, Flange, DNV | FT-1040-FD5 (1041 = Rev. B reserve, 1038 = no reserve) | 10 <= 12: stock 3 wk |

## Base prices (condition valid on 16 Nov 2026, per-position scale, FX, HALF_UP to cents)

- 10: 2026-07-01..2026-12-31, scale 15: 618.40. 20: scale 1: 642.00. 30: scale 20: 109.50.
- 40: condition 2026-11-16..2027-05-31 (starts on the scenario date; the old one ended 15 Nov), scale 1: 1,485.00.
- 50: USD per 100 pcs, scale 25: 29,443.00 / 100 = 294.43 USD / 1.28 = 230.0234375 -> 230.02.
- 60: USD per 1 pc, scale 10: 2,076.85 / 1.28 = 1,622.5390625 -> 1,622.54.

Order value for the approval: 11,131.20 + 7,704.00 + 2,628.00 + 11,880.00 + 6,900.60 + 16,225.40 = 56,469.20 >= 56,400.00,
so the discount is 4.5 % (factor 0.955).

## Surcharges, setup, packaging, bonus

Mass surcharge per piece = surcharge kg x EUR/t / 1000: 10, 20: 18.5 x 1.31 = 24.235; 30: 4.2 x 0.98 = 4.116;
40: 41 x 1.31 = 53.71; 50: 9.6 x 0.98 = 9.408; 60: 62 x 1.31 = 81.22.

SP-NORTH (1,234.57; weights 10: 18 x 20 = 360, 20: 12 x 20 = 240, 50: 30 x 12 = 360): exact 462.96375, 308.6425,
462.96375; floors 462.96 + 308.64 + 462.96 = 1,234.56; the last cent goes to the largest remainder, a tie of 10 and 50,
broken by ascending position: 10 = 462.97, 20 = 308.64, 50 = 462.96.

SP-SOUTH (777.77; weights 30: 24 x 5 = 120, 40: 8 x 45 = 360, 60: 10 x 70 = 700): exact 79.0953, 237.2858, 461.3890;
half up 79.10 + 237.29 + 461.39 = 777.78; the largest weight (60) absorbs -0.01: 30 = 79.10, 40 = 237.29, 60 = 461.38.
(Largest remainders would give 79.09 / 237.29 / 461.39 instead.)

Packaging: 10: ceil(18/4) = 5 x 14.50 = 72.50; 20: 3 x 14.50 = 43.50; 30: ceil(24/10) = 3 x 9.80 = 29.40;
40: 4 x 38.00 = 152.00; 50: 5 x 9.80 = 49.00; 60: 10 x 38.00 = 380.00.

Bonus PB-7: eligible goods 2,628.00 + 6,900.60 + 16,225.40 = 25,754.00; credit 10,000 x 1.5 % + 5,754 x 2.5 % = 150.00 +
143.85 = 293.85. Cents 29,385 split by goods: 2,998.516 / 7,873.500 / 18,512.984; floors sum 29,383; the two cents go
to 60 (.984) and 30 (.516): 30 = 29.99, 50 = 78.73, 60 = 185.13.

## Final unit prices (unrounded components, one HALF_UP at the end)

| Pos | Formula | Exact | Unit price |
| --- | --- | --- | --- |
| 10 (KG-B) | (618.40 + 24.235) x 0.955 + (462.97 + 72.50) / 18 | 643.46476 | 643.46 |
| 20 | 642.00 x 0.955 + 24.235 + (308.64 + 43.50) / 12 | 666.69000 | 666.69 |
| 30 | 109.50 x 0.955 + 4.116 + (79.10 + 29.40 - 29.99) / 24 | 111.95975 | 111.96 |
| 40 (KG-N) | 1,485.00 + 53.71 + (360.00 + 237.29 + 152.00) / 8 | 1,632.37125 | 1,632.37 |
| 50 | 230.02 x 0.955 + 9.408 + (462.96 + 49.00 - 78.73) / 30 | 243.51810 | 243.52 |
| 60 | 1,622.54 x 0.955 + 81.22 + (250.00 + 461.38 + 380.00 - 185.13) / 10 | 1,721.37070 | 1,721.37 |

Total: 18 x 643.46 + 12 x 666.69 + 24 x 111.96 + 8 x 1,632.37 + 30 x 243.52 + 10 x 1,721.37 = 11,582.28 + 8,000.28 +
2,687.04 + 13,058.96 + 7,305.60 + 17,213.70 = 59,847.86.

Breakdown fields (not scored by the eval scorer, but in expected.json): surcharge_per_unit = mass surcharge + (ordinary
+ pool setup) / pieces, before discount: 49.96, 49.96, 7.41, 128.37, 24.84, 152.36; packaging_per_unit: 4.03, 3.63,
1.23, 19.00, 1.63, 38.00; rebate_total: 0, 0, 29.99, 0, 78.73, 185.13.
