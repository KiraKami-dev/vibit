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

## Environment variables (all optional)

| Variable | Default | What |
| --- | --- | --- |
| `AGENT_SPEC_VARIANTS` | `fast,search,explore` | Which attempts race; `fast,thinking` = main's pair |
| `AGENT_SPEC_RACE_SECONDS` | 330 | Cutoff for the race, from the orchestrator's start |
| `SPEC_STEPS_FAST` / `SPEC_STEPS_THINKING` | 25 / 35 | Navigation steps of the fast and the thinking attempts |
| `AGENT_LOOP_WINDOW` / `AGENT_LOOP_REPEATS` / `AGENT_LOOP_STOP` | 10 / 3 / 3 | Loop guard |
| `AGENT_FORM_SECONDS` / `AGENT_SIGNING_RESERVE` | 360 / 180 | Form and signing time |
| `AGENT_MAIL_SECONDS` | 600 | Mail research time from the orchestrator's start |
| `AGENT_ERP_RACE` / `AGENT_ERP_SECOND_AFTER` / `AGENT_ERP_ALT_TABS` | 0 / 40 / 3 | ERP second search: on, seconds before it starts, how many at once |
| `AGENT_REVISION_JUDGES` | 2 | 1 = a single revision judgement |
| `AGENT_STRONG_MODEL` | unset | Model name for the `explore` attempt, e.g. one your provider key allows |
| `AGENT_STRONG_BASE_URL` / `AGENT_STRONG_API_KEY` | `OPENAI_BASE_URL` / `OPENAI_API_KEY` | Its endpoint and key (team secret) |

## What to look for in the next test run's log

- `specification variant_start` ×3, then `specification winner attempt=…` with `since_start` well before 5:30.
- `loop` events: which attempt looped, and whether it was stopped.
- `calc answer` with positions; the form's `configure_position` calls finishing before signing.
- `erp second_search_start` (should be absent when the first searches work), `erp second_search` with `added`.
- `revision_disputed` when the two revision judgements differ.
