# v3: changes against v2 (run_715aa132bf4a5c38)

Root cause of the 10/100 v2 run: the specification was found by one worker following one strategy; it went in
circles for 25 steps, its retry was the same attempt again, and without requirements every position was dropped.

| File | Change |
| --- | --- |
| `orchestrator.py` | The specification is a race of 3 attempts in their own tabs (fast, search-first, overview-first). The first with requirements wins, the others are cancelled; hard cutoff `AGENT_SPEC_RACE_SECONDS` (330 s from the orchestrator's start). The first revision whose requirements arrive is kept; other revisions are refused and reported. `FORM_SECONDS` 300 → 360, `MAIL_SECONDS` 540 → 600. A bid without positions still carries an unconditional discount. |
| `research.py` | `research_spec(hint=, strong=, race=)`: strategy text per attempt, optional strong model, loop guard on. Finding step 25 → 40 steps (`AGENT_FIND_SPEC_STEPS`). |
| `browser_tools.py` | State loop guard: the same page state (URL + text, refs ignored) 3 times in the last 10 views gets a warning; with `stop_on_loop` the 3rd warning raises `Stuck`. Off unless a worker asks for it. |
| `worker.py` | `run_worker(strong=, loop_guard=, stop_on_loop=)`; `Stuck` ends the run like a timeout. |
| `llm.py` | Optional stronger model on any OpenAI-compatible endpoint (`AGENT_STRONG_MODEL`). Unset: Qwen with thinking. |
| `submit.py` | `SIGNING_RESERVE` 240 → 180 s (`AGENT_SIGNING_RESERVE`); the form now has 180 s for its steps and positions, not 60. The signing fix from main (form deadline, no going back, sign-only last pass) is kept. |
| `llm.py` (defaults) | As in the submitted package: stream watchdog on by default (`LLM_STREAM_WATCHDOG=0` turns it off), call cap 150 s (was 300). |
| `tests/test_v3.py` | Loop guard, `Stuck` leaving a model run, the race (winner, cancel, cutoff). |

## Environment variables (all optional)

| Variable | Default | What |
| --- | --- | --- |
| `AGENT_SPEC_VARIANTS` | `fast,search,explore` | Which attempts race; `fast` alone = v2 behaviour |
| `AGENT_SPEC_RACE_SECONDS` | 330 | Cutoff for the race, from the orchestrator's start |
| `AGENT_FIND_SPEC_STEPS` | 40 | Steps for finding the document and its history |
| `AGENT_LOOP_WINDOW` / `AGENT_LOOP_REPEATS` / `AGENT_LOOP_STOP` | 10 / 3 / 3 | Loop guard |
| `AGENT_FORM_SECONDS` / `AGENT_SIGNING_RESERVE` | 360 / 180 | Form and signing time |
| `AGENT_MAIL_SECONDS` | 600 | Mail research time from the orchestrator's start |
| `AGENT_STRONG_MODEL` | unset | Model name for the `explore` attempt, e.g. one your provider key allows |
| `AGENT_STRONG_BASE_URL` / `AGENT_STRONG_API_KEY` | `OPENAI_BASE_URL` / `OPENAI_API_KEY` | Its endpoint and key (team secret) |

## What to look for in the next test run's log

- `specification variant_start` ×3, then `race_won variant=…` well before 5:30.
- `loop` events: which attempt looped, and whether it was stopped.
- `calc answer` with positions; the form's `configure_position` calls finishing before signing.
