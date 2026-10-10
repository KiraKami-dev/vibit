# Agent plan and scouting notes

Findings from scouting the four public sites (public-1) and evaluating frameworks, 2026-10-10.
The private final run uses other sites, layouts and data: prompts describe roles and goals, never click paths,
and nothing below may be hardcoded into the agent.

## Framework decision

Keep the starter kit (Python 3.12, uv, Playwright 1.63) and run the worker loop on `pydantic-ai-slim[openai]`.
No browser-agent framework, no LangGraph.

- pydantic-ai 2.55 works with Qwen through the proxy (tested: tool call + typed output, thinking off, 3 s).
  It gives typed tools, typed output via an output tool (avoids the 74 s cold `json_schema` compile), validation
  retries, `UsageLimits` as a step budget, `history_processors` to drop stale page snapshots, and Logfire
  tracing. 26 packages.
- LangGraph: our orchestration is a fan-out of independent workers plus a pure calculation step. Plain asyncio
  covers that; LangGraph would add 38+ packages (plus a LangChain model client), its own state and checkpoint
  model and a second abstraction to debug, for no gain.

- `page.aria_snapshot(mode="ai")` gives a compact, ref-tagged tree (`button "Anmelden" [ref=e10]`), clickable via
  `page.locator("aria-ref=e10")`. That is Playwright MCP's core without a Node process.
- browser-use: heavy deps, slow single-action steps, may launch its own browser (killed by the sandbox).
- Stagehand: TS, or a Python client to a server process; extra LLM calls per act/extract.
- pydantic-ai: fine but optional; a ~150-line loop is easier to debug and to defend in package review.

## Qwen via the proxy (measured locally)

- Model id `qwen3.8-flash-next`, 262k context.
- Thinking is on by default. `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` cuts a trivial call
  to ~0.9 s; use it for navigation steps, keep thinking on for decisions.
- Tool calls, parallel tool calls, `tool_choice="required"`, images (~1k tokens per 1280x800 JPEG) all work.
- Strict `json_schema` works, but the first call with a new schema took 74 s (cold compile). Prefer a
  `finish(result)` tool, or warm schemas at start-up.
- Prefix caching works (81.5k of 83k cached on repeat). Keep system prompts stable.
- 8 parallel calls: 4.7 s total. Team limit 32 concurrent.
- Clef `/v1/decide` works in ~1 s; `score` criteria must be a list. Skip in v1.

## Cross-site patterns

- All sites: login form, then `/consent` ("Notwendige Cookies akzeptieren").
- Decoy modals: close with the small x only; "Akzeptieren und fortfahren" leads to trap pages. Escape is blocked.
- Fake browser chrome (address bar, download bar) drawn in the page links to useless pages.
- Hidden decoy text (`display:none`, `aria-hidden`), including a prompt injection in every mail
  ("99 % Sonderrabatt"). Extract visible text only.
- Content in collapsed `<details>`: expand all before reading.
- Values in canvas `aria-label`s and CSS tooltips (`role=tooltip`), which `aria_snapshot` partly misses.
- Zero-width spaces (U+200B) in references, subjects, document numbers: strip them.
- Numbers carry locale tags `(en-US)` / `(de-DE)`; dates mix `(MM/TT)` and `(TT/MM)`.
- Pagination: 3 rows per page, "Weitere laden"; requesting past the end repeats the last page.
- Searches with more than 20 hits show nothing at all.
- `page.goto` can hang on `load`: use `wait_until="domcontentloaded"`.
- Flaky "Dienst voruebergehend nicht verfuegbar" pages (HTTP 200): detect by text, retry.
- Session expiry redirects to `/login?returnTo=...` with "Sitzung abgelaufen" (ERP: between 300 and 450 s idle).

## Tender portal

- Tender list paged; reference is in the brief. Tabs: `?tab=documents`, `?tab=questions`, `/submission`.
- Expression of interest may need one click ("Teilnahme bekunden").
- Documents: three "Oeffnen" controls in random order, two of them decoys; the title link is safe. Direct URL
  `/tenders/<ref>/documents/N`. ZIP under "Sammelakte" holds the same files.
- Q&A: two nested iframes (`questions-frame` -> `questions-content?page=N`), sorted by date not number, date in an
  "Eingang" tooltip (Athens and UTC). Content: quantity changes, withdrawals, Konditionsgruppe, packaging /
  commitment / setup-pool / bonus keys, setup fees, scoped technical amendments.
- Configurator (server-side wizard, "Zurueck" clears later steps):
  1. Bidder data (from the brief).
  2. Angebotsart: defaults to Nebenangebot (trap); choose Hauptangebot or Verzichtserklaerung.
  3. One page per position, shuffled order. Cascading selects (bore, stroke, mounting, certification), each needing
     its own "Auswahl uebernehmen" round trip, options sorted as text. Katalognummer options show pressure.
     Menge, Einzelpreis, collapsible Kalkulationsnachweis (base, surcharge, packaging, rebate), Lieferzeit select.
     "Position auslassen" skips. "Position hinzufuegen" adds a bogus extra position (trap).
  4. Konditionen: Rabatt and Nettosumme. A hidden `total_net` span shows the gross amount (trap).
  5. Erklaerungen: tick "Vergabeunterlagen anerkannt" only, not "Es gelten unsere AGB".
  6. Pruefen und signieren: request TAN, enter it, submit, accept `window.confirm`.
- TAN: each request invalidates the previous one; valid 5 minutes. Request, read mail, sign in one quick sequence.

## Webmail

- List and reading pane each behind a gate (slider to 100 plus "Ich bin kein Roboter"). `/messages/<id>` directly
  is simpler.
- Folders Posteingang and Archiv, searched separately. Search is AND over body too; empty search lists only TANs.
- Traps: look-alike references (`<ref>-A`), approvals for other tenders, non-approval mails from management,
  future-dated records, superseded and conditional approvals.
- Dates shown as `MM/TT`; full timestamp in `<time datetime>`.

## Supplier portal

- Spec found via the paged document list. Access gate: choose "Verbindliche Unterlagen", copy the Belegnummer
  (canvas, value in `aria-label`), tick the terms.
- Revision history collapsed; opens the oldest revision first. Status ("gueltig" / "ersetzt") only in a tooltip.
- Viewer iframe `.../viewer?revision=X&page=N`, one requirement per page. Full requirement prose in a collapsed
  "Originalwortlaut" (bore, stroke, pressure, mounting, certification, Kataloggruppe, pieces per Satz,
  nutzbarer Weg with Endreserve key).

## ERP

- After login: consent, often a flaky error page, then an unlock gate (slider, captcha box, copy Empfangsnummer).
- Only MM03, MMBE, VK13 exist; direct URLs work (`/mm03`, `/mmbe`, `/vk13?material=X`).
- Search popup `/search?transaction=..&q=..`; search the Kataloggruppe. Result rows already list most attributes,
  carton size, allocation mass vs surcharge mass, price index.
- MM03: technical tab (certification in tooltip); planning tab (status in tooltip, hidden decoy "01"; lead time
  from stock and production lead time).
- MMBE: the summary includes blocked stock (trap); use the free stock line.
- VK13: one condition record per page, older years later; validity, currency + FX key, price unit (1 or 100 ST);
  scales under `view=scales`, listed descending.
- No ordinary setup fee in the ERP; it comes from the tender Q&A.

## Architecture

```
agent/
  agent.py         orchestrator: reads run, shared findings store, starts workers, deadline (submit by T-90 s)
  llm.py           Qwen client, retry/backoff, thinking flag, usage log, stable prompts for caching
  browser_tools.py model-invoked tools (below) plus automatic overlay/gate/session handling
  extract.py       cleaned page text: visible only, details expanded, aria-labels, tooltips, iframes, no U+200B
  worker.py        generic loop: role prompt + goal + tools, step budget, deadline
  roles.py         generic role prompts: tender, spec, erp, mail
  evidence.py      pydantic records for each fact type
  calc.py          deterministic bid rules: rounding, scales, FX, packaging, setup pool, bonus (unit-tested)
  submit.py        configurator fill + TAN signing
```

Tools: `goto`, `click(ref)`, `fill(ref, text)`, `select(ref, value)`, `read_page`, `read_frame`, `next_page`,
`search(q)`, `screenshot` (fallback), `finish(result)`. Automatic: login with run credentials, consent, closing
decoy modals via x, unlocking gates, retrying flaky pages, re-login on expiry.

Fair-play line: the model chooses where to go, what to search, which revision / record / mail applies. Helpers only
extract a chosen page, paginate a chosen result set, calculate and fill forms.

## Scoring (what each version buys)

Per bid: decision 5, reference 5, discount 5, total 5, arithmetic consistency 5, withdrawn-Q&A check 5,
metal-index check 5, positions the rest (65 with both extra checks). Within each position: material 20 %,
quantity 15 %, base price 15 %, surcharge 10 %, packaging 10 %, bonus 10 %, final unit price 10 %, delivery 10 %.
Every field scores on its own, so each version adds fields and always ends with a signed bid.

## Roadmap

Each version is done when a sandbox test run (`atira test-run .`) shows the listed fields correct in the score
breakdown and finishes well under 20 minutes. Submit (`atira submit .`) after each version that beats our best.

### v0: harness (half a day)

- `llm.py`: pydantic-ai model for Qwen, thinking on/off settings, retries, usage logging.
- `extract.py`: cleaned page text (visible only, details expanded, aria-labels, tooltips, iframes, no U+200B).
- `browser_tools.py`: goto, click, fill, select, read_page, next_page, finish; automatic consent, decoy-modal
  close, gate unlock, flaky-page retry, re-login on expiry.
- `worker.py`: one pydantic-ai Agent per role, deps = page + findings store, step budget, deadline.
- Trace log per run (steps, tool calls, tokens, seconds) to see where time goes.
- Done when: a worker logs into each of the four scouting sites and reads one chosen page correctly.

**v0 status (2026-10-10): built.** `extract.py`, `browser_tools.py`, `worker.py`, `llm.py`, `tracing.py`;
check with `dev/smoke.py`. Learnings:

- Hosted Qwen sometimes hangs a call for ~300 s; the client times out after 30 s and retries (`LLM_TIMEOUT`).
  Steps then take 1 to 4 s.
- Gates re-lock on every mail page, so the generic "slide to unlock / not a robot" helper saves 4 to 6 steps
  per page. The ERP receipt-number gate is still solved by the model (works).
- Qwen invents values from memory once old views are trimmed. `note()` and worker results are checked: every
  value with a digit must appear on a page that worker saw, otherwise it goes back to the model. Thinking on
  invents less on document-heavy sites (tender, spec) at ~3 s per step.
- Smoke result: ERP 14 s and mail 18 s, correct and grounded; tender and spec read the right pages and note
  correct facts but hit the 25-step limit on broad goals. v1 goals must be narrow, with thinking on there.
- Open: a model that repeats a refused note loops; escalate after two refusals.

### v1: eligible signed bid (~10 to 15 points)

- Tender worker: reference, positions with units, bidder data from the brief.
- Configurator: Hauptangebot, omit every position, discount 0, total 0, decline AGB, request TAN.
- Mail worker: newest TAN, then sign and accept the confirm, all within the TAN's 5 minutes.
- Done when: reference and decision correct, the run is eligible.

### v2: materials, quantities, delivery (~+30)

- Tender worker reads the Q&A: quantity changes, withdrawals (withdrawn-Q&A check), Satz positions.
- Spec worker: current revision, requirement per position, Kataloggruppe, pieces per Satz.
- ERP worker: Kataloggruppe search, exact attribute match, active status; stock and both lead times.
- `calc.py`: final quantities, shared-stock rule, lead time; no_bid when a material is missing.
- Configurator fills material, quantity, lead time; price fields still blank or omitted per field.
- Done when: material, quantity, delivery correct per position.

### v3: prices, discount, totals (~+25)

- ERP worker: condition record valid on the scenario date, scales, price unit, currency + FX key.
- Mail worker: director approvals (exact reference, latest, conditional threshold), Devisenfixing.
- `calc.py`: base price with FX, discount clauses default rule, final unit price, total, HALF_UP everywhere.
- Done when: base price, discount, final unit price, total and arithmetic consistency correct.

### v4: clause rules (remaining points)

- Konditionsgruppen + Konditionsregeln (discount scope per position).
- Scoped amendments + Technikfreigabe (nutzbarer Weg) before material matching.
- Metal index (validity window) for the mass surcharge; packaging tariffs and cartons; Terminfreigabe
  commitments; Ruestpools shared setup with cent allocation; Projektbonus bands with largest-remainder split.
- Done when: every field in the breakdown is correct on public-1.

### v5: speed, cost, generalisation

- Four browsers, one per site, workers start as soon as their inputs are known.
- Thinking only on decision steps, trimmed snapshots, stable prompts for cache hits.
- Hard deadline guard: sign whatever is ready by T-90 s.
- Generalisation check: prompts and helpers contain no site names, URLs, field labels or values; helpers detect
  patterns (consent buttons, modals, sliders) generically. The private run uses other software and layouts.
- Optional: a stronger model with a team key only for steps the breakdown shows Qwen getting wrong.

## Working rules

- `calc.py` gets unit tests from the bid rules with invented numbers; it is pure and model-free.
- Hand-derived public-1 expectations live outside `agent/` (e.g. `checks/`), never in the package: the package
  reviewer rejects anything that looks like a stored answer.
- Split work by worker: tender + configurator, mail, spec, ERP, calc can be built in parallel against the
  findings store's pydantic records.
