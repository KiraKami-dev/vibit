# err404-nobid

Private team eval whose one correct answer is `no_bid`. Same site code as `../err404-traps` (starter kit plus Q&A tab,
Kataloggruppe search, dated mail with archive); only `world.js`, `BRIEF.md` and `expected.json` differ. All data is
invented.

```sh
atira eval dev . --port 8796 --json   # --json: the CLI's text output crashes on a no_bid score
atira eval deploy .                   # private; never --public
```

Scenario date 2026-10-09T10:00:00Z, tender VG-2026-0923, specification TS-0923 (Revision C gültig, B ersetzt).

## Traps

| Trap | Where | What a wrong agent does |
| --- | --- | --- |
| Match exists only before a scoped amendment | Q&A 2: Rev C, S-3 pressure 250 -> 280 | bids HZ-80-400-FU-0 (250 bar, active) |
| Only exact candidate after the amendment is blocked | HZ-80-400-FU-0H gesperrt | bids a blocked material |
| Look-alikes one attribute off | FL-0H (Flansch), FU-DH (DNV), 410-FU-0H (stroke 410) | bids a near match |
| Amendment to another revision | Q&A 4: Rev B, S-3 back to 250 | applies it, finds FU-0 |
| Superseded revision | Rev B S-3 = 280 | none (same result), but Rev B + Q&A 4 gives 250 |
| nutzbarer Weg (rule 20) | S-2 usable 300, Q&A 3 -> 310, Technikfreigabe Rev C ER-63 = 10 mm/end -> 330 | wrong stroke (300, 310, 320, 324, 334, 340, 350 all exist as active materials, so position 2 never decides the outcome) |
| Technikfreigabe for another revision / wrong domain / future | m-3010 (Rev B, 12), m-3012 (20), m-3013 (10.10., 15) | 334 / 350 / 340 (all active) |
| Conditional approval for no_bid | m-3003: 4 % if order value >= 20,000 | 4 % |
| Superseded approval | m-3002 (Archiv, 3 %) | 3 % |
| Future-dated approval | m-3005 (12.10., 5 % unconditional) | 5 % |
| Look-alike reference | m-3004 (VG-2026-0923-B, 6 %) | 6 % |

## Derivation of expected.json

- Decision (rules 3, 8, 14): Revision C is gültig. Its S-3 is 80 / 400 / 250 bar / Fuß / none. Q&A 2 (02.10.) is a
  scoped amendment naming Revision C and S-3: pressure becomes 280 bar; all other attributes are inherited. Q&A 4 names
  Revision B and does not apply. Required: 80 / 400 / 280 / Fuß / none. In Kataloggruppe KG-FU80 the only material with
  exactly these attributes is HZ-80-400-FU-0H, status gesperrt. No active exact match: `decision` = `no_bid`.
- Positions 1 and 2 match (HZ-40-200-FL-0; S-2: 310 + 2 x 10 = 330 -> HZ-63-330-GA-0) but do not change the decision.
- `positions` = [] and `total_net` = 0 (rule 8).
- `discount_percent` = 0: the latest exact-reference approval from `leitung@beispiel-hydraulik.example` no later than
  the scenario date is m-3003 (06.10.), conditional; for no_bid a conditional approval yields 0 % (rule 16). m-3002 is
  superseded, m-3005 is future-dated, m-3004 is another reference.
- Note: the eval scorer's no_bid breakdown is decision 75, reference 5, positions 5, total 15; it does not score the
  discount, so the approval traps here are not measured by the score.
