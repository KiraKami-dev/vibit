# v3: changes against v2 (run_715aa132bf4a5c38)

Root cause of the 10/100 v2 run: the specification was found by one worker following one strategy; it went in
circles for 25 steps, its retry was the same attempt again, and without requirements every position was dropped.

| File | Change |
| --- | --- |
| `orchestrator.py` | The specification is a race of 3 attempts (fast, search-first, overview-first), built on main's parallel attempts: each in its own browser context, requirements streamed to the ERP, a stopped navigator's last page still read. The first with a revision and requirements wins, the others are cancelled; hard cutoff `AGENT_SPEC_RACE_SECONDS` (330 s from the orchestrator's start). `FORM_SECONDS` 300 → 360, `MAIL_SECONDS` 540 → 600. (A bid without positions already carries the discount on main.) |
| `research.py` | `research_spec(hint=, strong=, race=)`: strategy text per attempt, optional strong model, loop guard on. Steps stay main's time-scaled budgets (`SPEC_STEPS_FAST` 25, `SPEC_STEPS_THINKING` 35, up to 1.4x). |
| `browser_tools.py` | State loop guard: the same page state (URL + text, refs ignored) 3 times in the last 10 views gets a warning; with `stop_on_loop` the 3rd warning raises `Stuck`. Off unless a worker asks for it. |
| `worker.py` | `run_worker(strong=, loop_guard=, stop_on_loop=)`; `Stuck` ends the run like a timeout. |
| `llm.py` | Optional stronger model on any OpenAI-compatible endpoint (`AGENT_STRONG_MODEL`). Unset: Qwen with thinking. |
| `submit.py` | `SIGNING_RESERVE` 240 → 180 s (`AGENT_SIGNING_RESERVE`); the form now has 180 s for its steps and positions, not 60. The signing fix from main (form deadline, no going back, sign-only last pass) is kept. |
| `llm.py` (defaults) | As in the submitted package: stream watchdog on by default (`LLM_STREAM_WATCHDOG=0` turns it off), call cap 150 s (was 300). |
| `tests/test_v3.py` | Loop guard, `Stuck` leaving a model run, the race (winner, cancel, cutoff, strategies). |

## After the first v3 test runs

| File | Change |
| --- | --- |
| `research.py` (revision) | `_choose_revision`: two judgements at once, one with the navigator's report, one from the page alone; a third call that sees both decides when they differ. |
| `research.py` (ERP) | `Erp._candidates`: an optional second candidate search by another route beside the prescribed catalogue-group search (`AGENT_ERP_RACE=1`). Off by default: in two sandbox runs where it started it never returned a list before the restarted first search did. `Erp._enter` is safe to cancel (tab slot given back, shared sign-in shielded). |
| `tests/test_parallel.py` | Both, without browsers or models. |

A second mail search by idle tabs was tried in one run (100, 14:16) and dropped: it started twice and was stopped
after 40 s and 7 s when the first search returned.

## Time (after three 100-point runs of 14 to 15 min)

Where the time went in those runs, and what was changed. None of it removes a check that changed a result.

| Wait | Seen | Change |
| --- | --- | --- |
| The mail planner waited up to 45 s for the tender's keys, then made its plan | The wait timed out in 3 of 3 runs; mail searches began 75 to 170 s into the research, with the tabs ready since about 25 s | `FINDINGS_WAIT` 45 -> 0: the plan is made at once from the brief and what is on the board. A plan call still out after 15 s (or failed) gets a second call beside it (`_mail_plan`) |
| Nine mail kinds queued behind 4 tabs | Three rounds of 40 to 190 s each | `MAIL_TABS` 4 -> 6: two rounds |
| Rework after the verifier | In 2 of 3 runs every mail fact was "doubtful" only because its quote is the message header; all kinds were searched again (145 to 170 s) and judged again (22 s), with the same result | The verifier says what a doubt is about (`Verdict.about`). Only a doubt about the record itself (reference, date, sender, superseded) sends a fact back; the second check runs only after a rework |
| Decision check before the form | 12 to 15 s; it can only turn a bid with a missing material into no_bid, which calc already does | Called only when it can change the decision |

## Two unchecked steps (after a 56-point run of the above)

That run lost its points in two single model steps, neither touched by the time changes: the spec reader copied
a wrong pieces-per-set for one requirement (26 pieces instead of 130, which also shifts the pool and bonus shares
of the positions next to it), and the form worker clicked past one position's page without entering it.

| File | Change |
| --- | --- |
| `research.py` | `_extract_page`: each specification page is read twice at once; where the readings differ, a third reader that thinks and is told what differs decides (`AGENT_SPEC_READS`, 1 = as before). |
| `submit.py`, `browser_tools.py` | `PositionDesk.premature` through `SiteSession.guard`: when the form worker is about to click or type on a page that shows exactly one position the bid has values for (not entered, not tried), it is told once to call `configure_position` first. Once per position, so it can never block the form; off in the signing pass. |

## The bid form (not yet run in the sandbox)

Measured before: the form pass took 167 to 352 s for five positions. Between every two positions the model was
asked once (3 to 7 s, more when a call stalled), twice where a worded choice matched no option, and the pages
after the positions took up to seven single steps.

| File | Change |
| --- | --- |
| `submit.py` | `configure_position` goes on by itself: after saving a position it enters the next position of this bid that the form shows, with the same mapping and buttons, until a page needs the model (a position to omit, a problem, the step after the positions, or the signing reserve). It only continues on a page that has the save button and a mapped control (`_form_on`), so a page that merely names a position is left to the model. |
| `submit.py` | A worded choice the model resolved for one position (control label and wanted value -> option) is reused for every position that wants the same value (`PositionDesk.worded`). |
| `submit.py` | When the last position is entered, the tool's answer says to do each following page with one `fill_in()` call. |

`AGENT_POSITION_BATCH=0` restores one position per call. The model still gives the mapping, names the buttons,
resolves worded choices, omits positions and does every other page.

## Environment variables (all optional)

| Variable | Default | What |
| --- | --- | --- |
| `AGENT_SPEC_VARIANTS` | `fast,search` | Which attempts race (`explore` is a third, overview-first one: it never won in five runs and is off to save cost); `fast,thinking` = main's pair |
| `AGENT_SPEC_RACE_SECONDS` | 330 | Cutoff for the race, from the orchestrator's start |
| `SPEC_STEPS_FAST` / `SPEC_STEPS_THINKING` | 25 / 35 | Navigation steps of the fast and the thinking attempts |
| `AGENT_LOOP_WINDOW` / `AGENT_LOOP_REPEATS` / `AGENT_LOOP_STOP` | 10 / 3 / 3 | Loop guard |
| `AGENT_FORM_SECONDS` / `AGENT_SIGNING_RESERVE` | 360 / 180 | Form and signing time |
| `AGENT_MAIL_SECONDS` | 600 | Mail research time from the orchestrator's start |
| `AGENT_ERP_RACE` / `AGENT_ERP_SECOND_AFTER` / `AGENT_ERP_ALT_TABS` | 0 / 40 / 3 | ERP second search: on, seconds before it starts, how many at once |
| `AGENT_REVISION_JUDGES` | 2 | 1 = a single revision judgement |
| `MAIL_TABS` / `AGENT_FINDINGS_WAIT` | 6 / 0 | Mail tabs; seconds the mail planner waits for the tender's keys |
| `AGENT_SPEC_READS` | 2 | Readings of each specification page; 1 = a single reading |
| `AGENT_POSITION_BATCH` | 1 | 0 = `configure_position` enters one position per call |
| `AGENT_STRONG_MODEL` | unset | Model name for the `explore` attempt, e.g. one your provider key allows |
| `AGENT_STRONG_BASE_URL` / `AGENT_STRONG_API_KEY` | `OPENAI_BASE_URL` / `OPENAI_API_KEY` | Its endpoint and key (team secret) |

## What to look for in the next test run's log

- `specification variant_start` ×3, then `specification winner attempt=…` with `since_start` well before 5:30.
- `loop` events: which attempt looped, and whether it was stopped.
- `calc answer` with positions; the form's `configure_position` calls finishing before signing.
- `erp second_search_start` (should be absent when the first searches work), `erp second_search` with `added`.
- `revision_disputed` when the two revision judgements differ.
- `mails plan` within about 40 s of the start; `verify evidence_only` instead of `verify rework`; no second
  `verify done` unless a rework ran.
- `spec_read_disputed` (a page read differently twice) and `guard` (the form worker was sent back to a position).
- `configure_position` events with `after=1, 2, ...` (positions entered in one call) and fewer model calls between them.
