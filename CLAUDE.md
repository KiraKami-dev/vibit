# CLAUDE.md

Team error404.ai at the Atira EHL Challenge ("From RFQ to bid", challenge id `rfq`, event 10 Oct 2026, up to 5 people
per team). The agent lives in `agent/` and is built on the Atira starter kit. Keep the GitHub repo private.

## Status and layout

- `docs/plan.md`: framework decision (pydantic-ai on Qwen, no LangGraph, no browser-agent framework), module layout,
  site scouting notes and the roadmap v0 to v5. Read it first. Its scouting notes describe public-1 only: they inform
  generic helpers, they must never become hardcoded paths, labels, values or prompt text (the private final run uses
  other software, layouts and data).
- `agent/`: starter kit (`run.py`, `browsers.py`, `models.py`) plus the harness (`llm.py`, `extract.py`,
  `browser_tools.py`, `worker.py`, `tracing.py`) and v1: `agent.py` `solve()` reads the brief (`briefing.py`), then
  `submit.py` signs a bid with every position omitted, fetching the contact address and TAN from the webmail.
  `calc.py` holds the deterministic bid rules (tested, not wired in yet). `agent.json` asks for 2 browsers.
- `dev/smoke.py`: the v0 check (one worker per site logs in and reads a page). From the repo root:
  `eval "$(atira scout --env)"; HEADLESS=1 uv run --project agent python dev/smoke.py [siteId ...]`
- Next: v2 to v4 for field points (research workers for spec, ERP, tender Q&A and mail, wired into `calc.py`). Use
  `git status` and `atira status` for the current state; do not trust dated numbers in this file.

## Tracking runs

Record every test run and submission; they go to `experiments/runs.jsonl` (append-only, `merge=union`, committed with
the change it measured). Local runs go to `experiments/local-runs.jsonl` (gitignored). `python3 dev/track.py report`
renders both to `docs/experiments.md` (gitignored). From the repo root:

```sh
dev/test-run.sh v1 "what changed" [experiment]    # sandbox test run, followed to the end, score recorded
dev/run-local.sh v1 "what changed" [experiment]   # local, headless; needs `atira models proxy`
python3 dev/track.py platform <sub_id> --version v1 --experiment NAME --note "..."   # after `atira submit`
```

`version` is the base being measured (v1, v2, ...); `experiment` names what a run varies on top of it (e.g.
`thinking-off-forms`, `model-<name>`, `4-browsers`), so variants of one version compare side by side. Environment
overrides (`AGENT_*`, `LLM_*`, `THINKING*`, `MAX_STEPS*`) are recorded, with secrets redacted. `atira test-run`
uploads untracked files too, so the recorded commit is a pointer; `atira package <id>` shows the exact upload.
When a run taught something (a failure mode, a speed-up, a score change), add a bullet to `docs/learned.md`. Notes
never contain credentials or values copied from the sites.

## Platform docs (source of truth; re-read, they change)

Base `https://ehl-challenge.atira.ai`. Public pages (home, start, challenge, leaderboard, team profiles) are also
Markdown via `.md`; docs are `/docs/<name>.md`.

- `/start.md`, `/challenges/rfq.md` (29 bid rules, answer schema), `/challenges/rfq/leaderboard.md`
- `/docs/fair-play.md`, `scoring.md`, `submitting.md`, `run-contract.md`, `building-an-agent.md`, `cli.md`, `api.md`,
  `mcp.md`, `faq.md`, `demo-day.md`; `/openapi.json`, `/llms.txt`, `/llms-full.txt` (all docs in one file)
- Announcements need a token: `curl -H "x-api-key: $(atira token)" https://ehl-challenge.atira.ai/v1/announcements`
- Submission deadline and prizes are not in the docs: ask the organizers. Demo day: best teams run live on stage, so
  keep a one-line start command (`atira test-run ./agent`) and a fallback recording.

The scouting logins are deliberately NOT stored here: run `atira scout`. Never write them, or anything copied from
the sites, into code, prompts, fixtures, notes or fallbacks.

## Fair play (an AI reviewer rejects or flags these)

- No hardcoded scenario values (tender refs, material numbers, prices, discounts, logins, TANs), not even as defaults,
  fallbacks or in prompts. Facts the brief states for every scenario (e.g. the sales director's mailbox) are fine.
- No pre-solving, by you or an AI assistant, put into the package. Hand-derived expectations stay outside `agent/`.
- Must be agentic: a model chooses where to go, what to search and what matters. Rejected: a fixed click path (model
  unused or only a fallback), or a crawler/helper that gathers all evidence before model-directed investigation.
  Allowed helpers: parse a page or document the model chose, paginate a model-selected result set, calculate,
  validate, retry, fill forms. Several agents and parallel browsers are fine.
- Only the platform API, the four sites and the model proxy; no local models; no own browsers in the sandbox (use the
  provided 1 to 4, tabs and contexts are free).
- Do not write notes meant to steer the reviewer. Outcomes: passed; flagged (runs, but hidden from the leaderboard
  until an organizer passes it); rejected (no runs).

## The task and its traps

One signed bid, entered in the tender portal's configurator and signed with a TAN from the webmail. Site ids are
public-1 names; the private set renames and re-splits sites, so never branch code on siteId, URL or layout.

| Site | Holds |
| --- | --- |
| Tender portal | Tender, bill of quantities, bidder Q&A (the hub: quantity changes, withdrawals, scoped spec amendments, Konditionsgruppe, packaging, commitment and setup-pool keys), configurator |
| Supplier portal | Buyer's spec and revisions (oldest shown first; use the one marked current) |
| ERP | Materials and status, stock, lead times, price conditions with validity and scales, pieces per carton, allocation mass |
| Webmail | Director's discount approval, TAN, authority mails (look-alikes, superseded, archive) |

Read all 29 rules in `challenges/rfq.md` before writing prompts or tools. Highest-impact traps:

- **Authority and time (21):** per key, the latest record dated no later than the scenario date, for the EXACT tender
  reference, from the right sender (technik, logistik, finanzen, kalkulation + bidder mailbox domain). Future-dated and
  unrelated records never apply; a failed condition does not revive an older record.
- **no_bid (3, 8, 16):** if any position has no exactly matching active material (after amendments and the "nutzbarer
  Weg" derivation, rule 20), send `no_bid`, no positions, `total_net` 0 (schema still requires `discount_percent`; a
  conditional approval means 0 %). Omitting positions is not a substitute (rule 9).
- **Sign and submit:** saving alone scores nothing. Partial bids use "Position auslassen" and are still signed.
- **Per position vs per material:** price scales per position; stock pooled per material (15); delivery commitments
  per position, only if the material matches and pooled demand is within the inclusive ceiling (24).
- **Rounding:** HALF_UP, at the steps the rules name: FX-converted base price to cents (22), discount threshold and
  bonus aggregate to cents (16, 26), then the final unit price once from unrounded per-piece components (27).
  Breakdown fields are reports, not inputs. Bonus split: largest remainders (26); setup pool: the cent-allocation rule
  stated in the Ruestpools mail (25).
- **Reporting:** `surcharge_per_unit` is before discount and covers mass surcharge plus ordinary and allocated setup;
  setup and packaging are never discounted; blank = unanswered, `0.00` = known zero; `rebate_total` is per position.
- **Conditional discount (16):** threshold uses undiscounted scale prices, no surcharge or setup, inclusive; on failure
  0 %, never an older approval. Metal index: the rate whose window contains the scenario date (28).
- Positions are matched by number; extras or duplicates cost one position's share; money is compared to the cent.

## Runs, ranking, winning

Details live in `submitting.md`, `run-contract.md`, `scoring.md`, `faq.md`. What bites:

- Test run (`atira test-run ./agent --follow`): 2 h, public data, shows per-part points, never on the leaderboard.
  Scored (`atira submit ./agent`): AI review, then one 20-minute run, one submission in flight per team (else 429).
- Time runs from the start of the run command to the accepted answer. 202 is final (even if wrong); 400 lists issues
  and the clock keeps running; validate against `answer_schema` first. Submit about 90 s before `expires_at` (the kit's
  `run.seconds_left` helps); platform docs only say "about a minute". A run without an answer scores 0.
- Setup has network and 5 min (untimed), but no run variables or secrets, and nothing it starts survives. The timed run
  reaches only the platform, sites and model proxy (`HTTP(S)_PROXY`, honour it in custom clients). Send your own
  User-Agent (default Python agents get 403 "Error 1010").
- `agent.json`: `setup`, `run`, `models` (list every hosted model used, e.g. `["qwen","clef"]`), `browsers` 1 to 4.
  Package: 20 MB compressed and uncompressed, 2,000 files, 5 MB per file, `.gitignore` honoured, no secrets inside.
  Own model keys are team secrets (names: capitals, digits, underscore, letter first, max 20, no platform names).
- Order: eligible (right tender reference), then quality 0 to 100, then lower cost x time (floors $0.01, 1 s). A wrong
  bid/no_bid decision costs quality, not eligibility (75 of 100 in a no_bid case). Weights: rule 29.
- Winners: private-dataset final run of our LATEST submission that PASSED review (not the best), then public
  leaderboard, then elegance. So: never tune to public-1; every passing `atira submit` replaces the final candidate,
  even a worse one, so confirm with the team first. `atira final --best-of 3` (before the final starts) allows three
  attempts (own-key cost up to 3x); private runs show no score breakdown.
- Models: hosted Qwen3.8-Flash-Next (`QWEN_BASE_URL/KEY`, 32 concurrent) and Clef (`CLEF_*`, 16 concurrent) are free
  but add a SIMULATED price to run cost; own-key calls are metered at provider prices through the proxy. Measured
  Qwen behaviour (thinking flag, schema cold start, caching) is in `docs/plan.md`.

## Dev loop

```sh
curl -fsSL https://ehl-challenge.atira.ai/install.sh | sh    # CLI; the installer provides Node 24 if missing
export PATH="$HOME/.local/bin:$PATH" && atira login           # `atira upgrade` if a command is unknown
(cd agent && uv sync && uv run playwright install chromium)
atira models proxy                       # terminal 1 (port 8765)
eval "$(atira scout --env)"              # terminal 2: scouting logins + model vars
(cd agent && uv run --no-sync python agent.py)   # local run, nothing submitted
(cd agent && uv run python -m unittest discover -s tests)
atira test-run ./agent --dry-run         # what would upload
atira test-run ./agent --follow          # sandbox run with score breakdown
```

Debugging: `atira logs <run> --follow`, `atira status [id]`, `atira package <id> --out dir`, `atira cancel <run>`; every
command takes `--json`. `claude mcp add atira -- atira mcp` gives `diagnose_run`, `run_logs`, `run_llm_calls`,
`run_screenshot`, `compare_runs` and more (`mcp.md`). Use the score breakdown to localise errors (zero surcharge
points can come from mass surcharge, setup or metal index inputs). A cancelled scored run ends its submission (ignored
by the leaderboard), so you may resubmit. Missing "OpenAI key" 401s mean no provider key arrived (team secret).
On macOS, python.org Python needs `Install Certificates.command` or `SSL_CERT_FILE=/etc/ssl/cert.pem`.

## Team conventions

- Git: commit author `josteng <jonas@stenglein.dev>`; imperative subject of at most 50 characters, body as short `-`
  bullets; no attribution trailers (no Co-Authored-By, no session links); never stage anything under `.claude/`;
  never push or open a PR without asking first.
- Never put real credentials, personal data or site values in tests, fixtures, docs or comments; invent placeholders.

<!-- entire-graph:begin -->
This repo has the entire-graph code graph installed. Before exploring code with
grep/find/whole-file reads, read .entire/graph-agent.md — resolution-first guidance
for using graph retrieval, focused source inspection, and verification.
@.entire/graph-agent.md
<!-- entire-graph:end -->
