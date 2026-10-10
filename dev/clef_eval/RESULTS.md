# Clef vs Qwen on agent decisions

2026-10-10, local proxy. Eval set: page views from our own local-run traces (public-1), hand-labelled, kept in
`data/` (gitignored: it holds site text). `data/build.py` makes `items.json`; `eval.py` runs it.
Each item: same page text and question for every model. Clef via `agent/clef.py`; Qwen
(`qwen3.8-flash-next`) as one chat call, thinking off or on.

Decision types:

- `choice`: which link or control on a view serves a goal (14 items; options = every link and button with its row).
- `noul_doc`: right document, current revision, exact reference, record dated no later than the scenario date,
  quantity stated (16 items).
- `noul_outcome`: is the page an error, empty result, locked, dead-end or decoy page after the last action (20 items).
- `prefilter`: which rows of a list (Q&A answers, mails, tenders) match a criterion (5 lists, 18 row decisions);
  Clef answers all rows of a list in one call with one `noul` question per row.

## Results (3 repeats each)

| Type | Model | Accuracy | Median s | p90 s | $ per decision |
| --- | --- | --- | --- | --- | --- |
| choice | Clef | 79 % (33/42) | 1.4 | 2.0 | 0.00028 |
| choice | Qwen, thinking off | **93 %** (39/42) | 1.3 | 2.0 | 0.00013 |
| choice | Qwen, thinking on | 86 % (36/42) | 6.4 | 106 (hang) | 0.00044 |
| noul_doc | Clef | 88 % (42/48) | 1.1 | 1.6 | 0.00028 |
| noul_doc | Qwen, thinking off | 92 % (44/48) | 1.1 | 2.0 | 0.00013 |
| noul_doc | Qwen, thinking on | **98 %** (47/48) | 5.2 | 10.5 | 0.00044 |
| noul_outcome | Clef | **100 %** (60/60) | 1.1 | 1.6 | 0.00015 |
| noul_outcome | Qwen, thinking off | 93 % (56/60) | 1.4 | 2.4 | 0.00008 |
| noul_outcome | Qwen, thinking on (14 items) | 100 % (42/42) | 4.3 | 10.3 | 0.00044 |
| prefilter | Clef | 100 % (54/54) | 1.1 | 1.6 | 0.00028 per list |
| prefilter | Qwen, thinking off | 100 % (54/54) | 1.2 | 5.2 | 0.00013 per list |
| prefilter | Qwen, thinking on | 100 % (54/54) | 5.7 | 14.2 | 0.00044 per list |

Cost is the simulated price: Clef $0.24 per M input tokens (~1.1k tokens per page view), Qwen $0.15 in /
$0.47 out. All are negligible next to a worker step (~10k+ tokens); latency and accuracy decide.

## Where Clef helps and where not

- Clef is deterministic: the same misses on every repeat. Qwen off varies between repeats.
- **Outcome checks: Clef wins.** 60/60 with a wide margin (dead ends p >= 0.68, real pages p <= 0.09) at ~1 s.
  Qwen without thinking missed an unloaded reading pane every time and a "TAN requested" page once; Qwen with
  thinking is also exact but 4x slower.
- **Link choice: Clef loses.** It misses choices that need ordering or reading a row's tooltip: "highest request
  number" (picks a lower one), "the newest is on the previous page", "the revision marked valid in a tooltip".
- **Right document / record: Clef loses** to Qwen with thinking. It took a look-alike reference (`<ref>-A`) for the
  exact one and misread a stated quantity; Qwen off also fell for the look-alike.
- **Pre-filtering rows: tie** (all 100 %). No gain over Qwen, and Qwen already reads short lists in one step.

## Integrated

Only the outcome check, off by default: `AGENT_CLEF=1` (alias `USE_CLEF=1`; the `AGENT_` prefix is recorded by
`dev/track.py`) gives every worker a `page_check(last_action)` tool. `clef.outcome_hint(view, action)` is ready for an
automatic check after actions, not wired in. Not yet measured inside a full run.
