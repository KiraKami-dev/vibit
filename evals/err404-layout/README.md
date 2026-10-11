# err404-layout (private team eval)

Same RFQ task, rules and answer schema as the challenge, in a different shape, to test how the agent copes with the
private final dataset's "other software, layouts and data". All data is invented.

| Site | Holds |
| --- | --- |
| `sourcing-hub` | Notices (with a look-alike `-B` lot), "Register to bid", bill of items, the bid form |
| `buyer-extranet` | Specification TS-4471 (Rev. A shown first, Rev. C current) AND the bidder clarifications (Q&A) |
| `ops-suite` | ERP in English: search (max 20 hits), per material General / Stock & lead times / Pricing tabs |
| `inbox` | Mail with Inbox and Archive, oldest first, six per page, AND search; signing codes arrive here |

What differs from public-1: English labels everywhere; Q&A on the buyer's site, not the tender site; sidebar
navigation; the bid form is a table of three lines per page with repeated labels ("Qty", "Unit price"), a series
(catalogue group) select, an ARIA combobox for the material, a radio group for lead time and the bid type, English
numbers ("1,234.56"); six positions; USD conditions with "1 EUR = x USD" fixings and a per-100 price unit; two setup
pools with different cent rules; a marginal project bonus; metal indices in EUR per tonne with monthly windows; a
quantity change plus a withdrawn one; a "Rev. C" scoped amendment and a decoy amendment to Rev. B; sets of 4 pcs (5 in
older revisions); a production lead time in working days; a conditional discount that just passes; a price condition
starting on the scenario date; usable stroke with a revision-specific end reserve.

- `world.js`: the data. `DERIVATION.md`: how `expected.json` follows from it. `derive.py`: recomputes it in Decimal.
- `atira eval dev . --port 8810` runs it locally; `atira eval deploy .` (private) uploads it.
