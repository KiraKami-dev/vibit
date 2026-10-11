# What we tried and learned

One bullet per experiment or failure: what changed, what happened, what we keep. Written by hand; run data is in
`experiments/runs.jsonl` (render it with `python3 dev/track.py report`).

- v0-starter test run: the starter kit's `solve()` stub exits without an answer; the sandbox scores that 0 and
  ineligible after 4 s.
- v1 local (first signed bid): the configurator worker needed 36 requests and 433 s for a bid with every position
  omitted; about 230 s of that went to the TAN round trips (153 s to 385 s).
- TAN loop: each TAN request invalidates the previous TAN, and the mail worker was started 6 times (2 hit the 8-request
  limit) before a fresh TAN reached the signing step in time. Fetch the TAN right after requesting it, in one sequence.
- Tokens: 247k input for a bid without positions, nearly all in the configurator worker; trim page views before v2.
- TAN lookup fixes: never put wall-clock time in a goal (the sites run on the scenario clock, so a TAN "requested at
  <today>" never shows up); "newest" is the highest request number. Short lookups run without note(), so the model
  returns its result instead of noting it and searching on.
- v1 first test run crashed at import: `llm.py` imported `httpx`, but openai 3.x depends on `httpx2`; the local venv
  still had a stale `httpx` from openai 2.x. Run `uv sync` after dependency changes so local matches the sandbox.
- v1 first scored test run: quality 10 (decision 5 + reference 5), eligible, 3.4 min, $0.022. Discount, total and
  total consistency score 0 even with every position omitted and total 0; positions need v2.
- v1 submission: review passed, quality 10, 4:41 min, $0.024; team rank #4. Submit from a clean `git archive` export
  of agent/ so unfinished files never reach the package reviewer.
- The scouting logins are global demo logins shared by every team: local bid-form and TAN runs collide with other
  teams (TAN request numbers jump). Verify TAN signing with test runs, which get fresh logins (up to 5 per team at
  once, 3 more waiting); see the isolation probe below for what else is shared.
- Isolation probe (two browser contexts, scouting login): the bid wizard's progress lives in the session cookie, not
  the account (A advanced to step 2, a fresh context B still saw an empty step 1), so local bid-form runs do not see
  each other's wizard. The webmail is one shared mailbox (same list in both contexts, TAN mails numbered across all
  teams), so TAN requests remain the only collision: stop local runs before the TAN request, sign in test runs.
- Scouting logins are shared by all teams: TAN request numbers are global and a request from another team
  invalidates ours (local runs saw "Anforderung 59" rejected after someone requested 60). The bid wizard state is
  per browser session, so local runs are fine for the form phase; measure signing only in test runs
  (`AGENT_STOP_BEFORE_SIGN=1` stops a local run before the TAN request).
- Bid speed-up, test runs: baseline 201 s; two phases (form thinking off, review thinking on) 271 s (one run:
  documents detour, mail lookup hit its step limit, slow thinking steps); review thinking off too 171 s;
  final variant 94, 96, 105 and 87 s (4 runs), quality 10 each, cost $0.008 to $0.010 instead of $0.019.
- Final variant: form phase thinking off with fill_in() (several form actions per step), then one thinking-off
  model call checks the review page, then the signing phase (thinking off unless the check fails or finds
  problems) uses request_signing_tan(ref), which clicks the model-chosen button and fetches the TAN in one tool.
- Summary check offline test (6 doctored review pages: ok, alternative offer, 5 % discount, position not
  omitted, extra position, transposed VAT id): thinking off 6/6 in 2 to 5 s, thinking on 6/6 in 4 to 27 s.
- Briefing with thinking off is correct (validated against the brief) and takes 6 to 10 s instead of 17 to 45 s.
- Thinking-off form steps sometimes click the wrong ref on the one-position-per-step configurator ("add
  position" instead of "next"); the summary check and the goal's "never use options that add positions" catch it.
- Goal wording costs time: "acknowledge the tender documents" sent the model to read every document (35 s);
  "tick the form's acknowledgement (no need to open them)" fixed it.
- After the bid is in, the platform closes the run's model token; the final model call gets 401. Caught now.
- Faster v1 submission (thinking by phase, fill_in, TAN hand-off): quality 10 in 2:34 min for $0.011, against 4:41 and
  $0.024 for the first v1; no quality loss, cost x time about 4x better.
- Page views, read-only bench (smoke goals, 3 runs each, base vs new): the history trim kept only the newest request's
  view, so after any note() the model had no page at all and invented values (refused notes, re-opened pages). Now
  the latest view stays until a newer one arrives, and the first prompt's view is trimmed once superseded.
- Same-page actions now return only the changed parts (controls are separate pieces, unchanged refs stay valid) or
  "unchanged"; the full view stays in context and in the cached prefix. Invented form (fill, select, tick, save):
  ~27.7k -> ~20.6k input tokens, correct in 7/7 runs. Read-only sites navigate, so diffs rarely fire there.
- Select lists over 600 characters show count, first 8 and last 2; `options(ref, contains)` searches them and a
  failed `select` names close matches. Costs one request when the model wants the list, saves tokens per view.
- Request makeup (smoke): system + tools ~5k chars fixed, history ~3k, view ~1.5-2.5k; 40-55 % of input is reported
  as cached, so per-request token cuts are modest; the step count dominates tokens and seconds (browser actions
  take 0.3-0.7 s, a model step 1-4 s). Repeated boilerplate lines (footer, title bar) were 1-4 % of view text when
  restricted to safe lines: not worth the risk of hiding status lines, dropped.
- Clef vs Qwen offline eval (`dev/clef_eval/RESULTS.md`, 68 hand-labelled decisions from real page views, 3
  repeats): Clef answers in ~1 s and is deterministic, but loses to Qwen at picking links (79 % vs 93 % thinking
  off) and at judging the right record (88 % vs 98 % thinking on; it takes `<ref>-A` look-alikes for the exact
  reference and misses ordering such as "highest request number"). It is exact at "is this an error, empty, locked
  or decoy page after my action" (100 % vs 93 % for Qwen off, same speed). Kept only that check, behind
  `AGENT_CLEF=1` (`page_check` tool); Clef's `score` criteria must be a list, and many `noul` questions fit one call.
- Site tools (`site_tools.py` search / collect / extract), local read-only check against the scouting sites: an ERP
  search that the site refuses as too broad took 10 steps, 46 s and 34k input tokens with the tools (remark reported
  right) against 30 steps, 153 s, 112k and no answer without; a mailbox search over inbox and archive took 18 steps
  and 105 s with the tools (10 messages over 3 pages each, newest right), plain stopped at 30 steps or hung on
  re-locking gates. Two fixes were needed: older tool results were cut to one line once a newer view came, so the
  model paged again by hand to see the list it had collected (results of search / collect / extract now stay), and
  after paging the next view must come whole, not as a diff against a page the model never saw.
- v2 position filling (`configurator.py`, `PositionDesk` in `submit.py`): the model names the position on screen,
  maps control labels to value names once and names the confirm button; the helper walks the cascading selects
  one level per round trip, matches options by value (text-sorted lists, neighbouring numbers or a catalogue
  number that is a prefix of another) and formats German numbers. Offline mock and live (scouting, up to the TAN
  page): one position entered in full, a position with an impossible attribute failed and was omitted, net total
  from the entered positions only, Hauptangebot, AGB unticked; 202 s, 28 requests, 183k input tokens.
- The worker forgot the page after a note(): `_forget_old_views` kept only the last request, so a short tool
  result wiped the newest view and Qwen clicked guessed refs (hit "add position" in the mock). Keep the newest view
  in full whatever comes after it.
- Attribute wording can differ from the option text (a "none" attribute vs a "without ..." option): the tool
  reports the options and the model passes `choices`, used only when no option matches literally (an always-on
  override broke a later position that was already right).
- Without "every value is already calculated; do not research" the bid worker wandered into documents and the Q&A
  and burned its 76 steps picking options itself. The goal now says so up front.
- Phased flow (form phase thinking off) on the offline mock: positions, discount, net total, offer type and
  declarations right, summary check matched, signed in 93 s and 16 requests. Thinking-off slips seen there and
  live: the model typed its own net total (now stated in the goal), clicked "omit" on a position without asking
  configure_position() (goal: call it first on every position page), sent a partial `fields` map that replaced
  the first one and let an unrelated position count as entered (maps now merge; a position counts only with
  material, quantity, price and lead time mapped), and "chose" a neighbouring bore for an impossible one via
  `choices` (numbers must now match exactly; `choices` only for worded attributes, remembered per position).
- Live on the shared scouting login, a re-login mid-run reset the wizard to its first step; the worker re-entered
  the form from the start. Another reason to verify signing only in test runs.
- Live, thinking off, the model still entered its own "all positions" net total after one position failed and
  clicked "omit" instead of "next" right after a successful fill (that position was lost). Now every
  configure_position() result states the running net total of the positions entered, and the tool saves the
  position with the next button the model named once. Mock after that: signed and fully correct in 64 s,
  13 requests.
- Local live runs twice hung before the briefing finished: one site's start (the webmail) never returned while the
  sites answered curl at once. solve() gathers every site start without a timeout; worth a guard.
- Streaming through the models proxy (raw SSE probes): text and `reasoning_content` arrive token by token (gaps
  under 1.5 s), but tool-call arguments are held back until complete (a 60-row output tool: first argument byte at
  64 s, all at once); quiet stretches carry `: keepalive` comments every 10 s. Headers come with the first (role)
  chunk: within 9.4 s in 179 of 180 calls (p90 5.9 s, either mode). The one hang (thinking off) sent no headers
  and nothing else for 200 s, so "no first chunk in 20 s" catches it; a tool call's silence cannot be told from a
  stall, so tool requests keep their per-mode timeout as allowed silence.
- `LLM_STREAM_WATCHDOG=1` A/B (30 agent calls each, run side by side): excluding one prompt that sends thinking-on
  reasoning into minutes, median 13.2 s vs 14.4 s, p90 74 vs 81 s, max 89 vs 268 s (deadline retried a legit
  thinking-on long answer twice); 2 no-first-chunk retries caught at 20 s. On the runaway-reasoning prompt the
  deadline failed 2 of 8 after 6 retries x 90 s, the watchdog finished 8 of 8 but took 87 to 811 s (6 calls hit
  the 300 s cap). Progress does not end runaway reasoning: keep the cap lower (LLM_MAX_CALL 120 to 150) or cap
  output tokens before turning the watchdog on.
- v2 orchestrator local runs (planner -> board -> workers -> verify -> calc): the planner picked all four fact kinds
  and mapped each to the right site from the brief alone (23 s); the tender worker resolved 5 positions with final
  quantities and keys in 75 to 105 s; the mail plan named 9 record kinds. `Trace.event()` takes `kind` and `worker`
  positionally, so board events must not pass keywords of those names (two runs lost every posted fact to that).
- The spec worker (`research.py`, thinking off) twice looped between two controls on one page for its full 25
  steps and found no revision history, so no requirement reached the ERP; it needs a loop guard or thinking on
  for that step. Mail kinds that find nothing burn the full 20-step budget (about 70 s per kind and tab).
- A thinking verifier call over a batch of facts can outlast a 120 s budget; verify runs thinking off with the
  long-timeout reader model by default.
- v2 end to end, local (planner -> board -> tender, spec + ERP, mail -> verify -> calc): one run reached the full
  answer (local check 100/100) in 752 s of research; other runs lost the spec (navigator paged between two list
  pages for 25 steps, or clicked row menus the popup closer shut), the mail tabs (sign-ins hung or landed on the
  browser error page) or the ERP (a look() wrapper missing the new keywords). Each now has a guard: a loop hint
  after three arrivals on one page, one thinking retry for the spec, time-limited starts with a retry that also
  rejects error pages, staggered mail tabs that join when ready, and a mail deadline with partial results.
- Mail records only count with the bidder's domain: when the address lookup ran on a tab that showed an error
  page, every authority record was dropped silently. The address now comes from the first tab that shows it.
- A requirement whose stroke reserve was not found came back without a material and calc turned the whole bid
  into no_bid. Undecided materials (pending inputs, search problems) now omit the position; only a complete
  search without a match means no_bid.
- One run with the mail kinds on site_tools.search/collect kept nothing in three kinds (150 s each; paging
  followed a message list into a session expiry); kept on the paging tool for now.
- First v2 sandbox test run (orchestrator branch 405b1f4): quality 100, eligible, about 10 min, 308 model calls,
  $0.134. The full pipeline works in the sandbox; the variance across repeats decides what to submit.
- v2 round 1 (4 sandbox test runs of 405b1f4): 100 (10 min), 100 with the watchdog (15 min), 10 (spec navigator
  found no revision history, so every position was omitted and the found discount was lost too), and NO BID: a false
  summary-check alarm switched signing to thinking, the model went back in the wizard (clears later steps) and the
  deadline cut it before the TAN was entered. Signing needs its own time reserve and must never go back.
- sub_9bd976b00673121a (quality 100, 14:02, rank #2) was the watchdog test run promoted to a submission from its run
  page. Its exact code is branch submitted/sub_9bd976b0. It still has the old signing weakness (no bid when signing
  overruns), so replace it once the fixed version scores 100 reliably across several test runs.
- Soak run 1 (3f5d6b6): NO BID. The sites were slow (several tabs hit the 45 s settle cap at once), the webmail
  session failed both starts and lost its tab, and submit_bid's address lookup then crashed on the empty tab list
  (IndexError), which ended solve() before signing. Fixed: sessions reopen a lost tab, mail lookups never raise,
  solve() retries signing once after an unexpected error.
- Spec redundancy (branch spec-redundancy): a fast and a thinking spec navigator run at once in separate contexts,
  the first with a revision and requirements wins; a stopped navigator still gets its revisions read from its last
  page. Tender documents and Q&A run in two tabs and positions post before the Q&A; a run without complete positions
  still answers (reference, decision, discount, total 0). Test runs: 100 (12.5 min, $0.177) and 100 (13.5 min, $0.184); one earlier run had a
  full research answer (82 offline) but signing timed out (fixed on main by the signing reserve).
- v3 (three spec strategies, loop guard, cutoff) on main de33a85, first test run: 100 in 15:04, $0.173, 363 calls.
  The fast attempt won the spec race 51 s after the orchestrator started; ERP searches took 12 to 34 s each and
  the materials were read by about 130 s; nine mail kinds over four tabs took 37 to 90 s each (done by 278 s);
  a mail rework after verify cost another 145 s. Bid form: 352 s, of which 172 s were two slow page loads of the
  bid site and 46 s a stalled model call (caught by the stream watchdog); the five positions took 83 s; summary
  check plus signing 50 s (TAN round trip 12 s). The form and signing times were enough with 207 s to spare, but
  only because research ended 130 s before its deadline: with a slow bid site the form needs about 350 s.
- Two starts before that failed on the platform side ("platform failed, not your agent"): such runs do not count.
- After the accepted bid the model token is revoked: the signing worker ends with a 401 and the sign-only last pass
  starts and fails at once. Harmless, but it reads like an error in the log.

- Mail research, local (scouting, 4 tabs): before 7 of 9 kinds in 515 s (plan 97 s after a 45 s stalled call;
  stalled reader calls held kinds 180 s; approval 330 s). After pipelined reads, a 60 s grace after the first
  hit, shorter read/plan timeouts and a fresh tab after 45 s without a page view: 8 of 9 in 401 s (plan 20 s,
  approval 106 s). Session expiry while opening a message's reading pane gave the reader a sign-in page (empty
  record); messages are now reopened when the tab landed elsewhere. Without the mailbox address, records are
  authorised by the recipients' domain instead of all being dropped.
- Second searches, one run (100, 14:16, $0.144, slow platform with 56 stalled calls): the ERP second search
  started for three requirements and never returned a list before the restarted first search did; the mail one
  started twice and was stopped after 40 s and 7 s. ERP one now off by default, mail one dropped.
- Where 14 to 15 min went (three 100 runs): mail searches began only 75 to 170 s in (a 45 s wait for the tender's
  keys that always timed out, then the plan call); nine kinds over 4 tabs took three rounds; the verifier called
  every mail fact doubtful because its quote is only the header, which re-searched all kinds (145 to 170 s) and
  changed nothing; form 190 to 350 s, signing 30 to 50 s. Spec (45 to 170 s) and ERP (done about 80 s later)
  were never the longest chain.
- The 7:33 submission is a promoted soak run of main: the same code took 7:33, 11:39 and 14:00 in other runs, so
  run time swings by a factor of two with the platform's speed.

- sub_63a81b12d47a68b9 (promoted from soak run run_262c91398cd64598): quality 100 in 7:33 for $0.110, rank #2.
  Final candidate since 22:17 UTC; exact code is 8c7ab8f (branch submitted/sub_63a81b12). Same code over 6 soak
  runs: 100, 100, 100, 100, 59, 37.1.
- ERP reliability, local read-only checks (shared scouting login, so session drops from other users are frequent):
  base ERP stage 288 s with three detail workers at the 12-step limit and a 95 s reselect (one thinking-on call
  hung to its 90 s timeout); with search/collect for the group search, open_pages that lists unopened links and
  skips views already read, reused tabs, serialised sign-ins and one gate pass per drop: 209 s, searches in 3 to 5
  steps, details in 4, reselect 4.5 s (choices that still pass check_choice stand without a model call). Same
  materials and records as the hand derivation.
- The ERP search worker looped when its hit did not fit the requirement it was shown (A-2 before the amendment and
  reserve: 507 vs 500): it refined and re-ran the search for 16 steps and the position was omitted (test run 51.2).
  The search goal now only asks for every hit of the group (no requirement shown), and a stopped search worker's
  hit list is read from its kept views. Gate recovery runs only on a sign-in form or a known gate path, since some
  views redirect and looked like a dropped session.
- Partial runs of 8c7ab8f (37, 59, 51 and soak 19, 31, 10): the bid form shows positions in its own order, and the
  thinking-off form model named the next position of its sorted list; configure_position said "not on the page"
  and the model clicked "omit" on the shown position, which had values (3 of 6 runs; reproduced live). It now
  enters the bid position the page shows and lists the ones still to enter; early returns are traced.
- Other causes in those runs: a spec model copied "1 set = 5 pieces" and parse_int took the 1 (a set position bid
  at a fifth of its pieces, shifting setup pool, bonus and packaging of the other members); a guessed control label
  for an attribute the form has no control for failed whole positions; one live run mapped no price breakdown
  fields at all; under model load (p50 11 s, p95 91 s) every 30 s try of the mail plan timed out and no mail kind
  was searched. All fixed on branch worktree-agent-a525dcbc767c5a2b1.
- Bid form overlapped with the research (BidFlow): an early pass enters bidder data and the main offer and maps
  the first position page's controls (~30-50 s, during research), then waits. Sandbox run 07b5 (quality 100):
  form 91 s after research (132-185 s before), signing 30 s, signed at ~620 s. The wizard keeps its step through
  a 180 s idle tab and through in-page fetches of the start page; locally the session drops every 1-3 min while
  other runs sign in with the shared scouting login (wizard back to step 1), which the form then redoes.
  Keepalive must run inside the tab: context.request resolves no hosts in the sandbox (EAI_AGAIN).
- confirm_decision after calc could never change a decision (calc returns no bid with missing materials) and
  cost ~8 s before the form; now skipped. A grounded FormReady quoting our own "5,00" was refused four times
  (30 s) and the model then went back a step; form status results are no longer grounded.
- Hung starts (run_9f6e6e23f55952fc, quality 30.6; the Haiku run lost 210 s the same way): in one browser context the
  webmail's requests never completed (first a goto that never committed, then a sign-in post), while a fresh context
  opened it in under 1 s. Playwright page calls (locator.count, evaluate) on a tab whose navigation has not
  committed wait for ever, so every settle hit its 45 s cap and the attempt waited 210 s on a dead tab. Starts now
  race a tab in a fresh context after 15 s without a usable page, give up a page that answers no script call for
  20 s, retry at once in a fresh context with 45/60/90/120 s budgets, and accept a rendered page whose settle never
  ends. Test runs of 86b4f27: 100 (7:33) and 100 (11:33), all starts in 2.4 to 4.6 s; the second still had three
  mail gotos mid-research that never committed, now bounded (30 s goto + about 22 s settle) and traced as slow_page.
- Synthetic scenarios (`dev/synth`, invented data on four local mock sites, look drawn per seed): 3,000 seeds give
  the same answer from an independent rule implementation and from `calc.py`. First agent runs (v3 working tree):
  seed 7 scored 10 (false no_bid), then 15 and seed 12 scored 57 after two fixes. What they showed: revision labels
  were matched as text ("III" against "Revision III vom <date>"), so a reserve that was read was never applied and
  amendments would be dropped (`evidence.revision_id`); an ERP hit list without attribute columns came back as the
  one hit the worker opened, which read as "search complete, no material" (search goal now asks for every row).
  Still open: a record kind with no mail in the mailbox burns its 240 s budget twice; ERP detail workers hit their
  12-step limit on a material whose data sits on five tabs; research then takes 650 to 750 s, so the form phase runs
  out (seed 7: all positions omitted at signing, seed 12: two of five). The mock has no gates, iframes or cascading
  selects, so it is easier than public-1 in those respects and harder in these.
- Generation 2 of the synthetic set takes its surface forms from a sandbox run log of ours: ids that say nothing
  about order (revisions, Q&A numbers, keys), records dated hours apart, small quantities, a stated number notation
  per mail, unused keys beside the used ones, a mail reading pane, an access-number gate, hex TANs. Seeds mean other
  scenarios than in generation 1. The log also caught a bug in the new `revision_id`: it split "R-1234" at the
  hyphen and so took every such revision for the same one; identifiers keep their hyphen now (tested).
- Generation 3 of the synthetic set has 33 named datasets (`dev/synth/run.py ... --dataset NAME`; list in
  `scenario.DATASETS`): 28 of them push one kind of oddness each (one or twelve positions, exact ties, boundaries met
  or missed by one, minute precision, long version chains, decoy floods, strange ids, foreign number notations, a
  release taken back, ...). Each passes oracle against `calc.py` and a bid-form round trip on 60 seeds; no agent
  run on them yet.
- A run that sat silent for 328 s (cancelled at 8:14) and an earlier one that took 14:40 and sent a wrong no_bid had
  the same cause: one ERP tab whose page stopped answering. Nothing bounded it: the sign-in check behind
  `open_pages` (`locator.count()`) has no timeout of its own, a start whose `new_page()` never returned was waited
  on without a limit once its racing tab had failed, and a material worker's deadline was the research deadline
  (590 s for one material that takes 15 to 60 s). The requirement then had no record and the bid turned into a
  no_bid. Now every such call is bounded, a read-only tab that stops answering is replaced by a new one that opens
  the address again, a dead tab is not handed to the next worker, and an ERP worker whose tab shows no new view
  of an answering page for longer than one navigation may take is ended (judged by progress, so a slow website is
  not cut). The platform's llm-calls list shows such a hang at once: no model call for minutes.
- Tokens are not what makes a run slow: about 1.0 to 1.2 M prompt tokens a run ($0.11 to $0.15), nearly all in the
  research (mail searches about a third, ERP about 30 %, bid form 8 %, specification 7 %). A third of the prompt
  tokens are cache reads; cache writes are always 0 for the hosted model (its cache is implicit and free to write).
  In the form and signing phases the time goes to model calls that never start (20 s each until the watchdog
  retries; 5 in one run's form and signing) and to bid pages that take the full 45 s to settle.
- Model calls start fast or not at all: over five runs the first chunk came within 4.1 s in 99 % of 344 calls (max
  11.3 s), and the platform's own queue time was over 5 s in 0.6 % of 1,453 calls. A call that never starts cost
  20 s until the watchdog retried it, which in the one-step-at-a-time phases (bid form, signing, TAN) is 20 s of
  the run each time. A second copy now goes out after 6 s (`LLM_HEDGE_AFTER`, 0 turns it off), at most four at once.
- The TAN lookup needs 6 to 8 model steps and had 10: in 2 of 5 runs it ran out while paging back and forth, and the
  bid form had to request another TAN (22 s and 105 s lost). It has 16 now and the loop guard's warning.
- A slow bid page is the website holding its answer, not our tab: the recording of one run shows the bid tab loading
  for 150 s after one click, while the model took three steps of about 47 s each on a page that answered nothing.
  Nothing was changed for it: a second request in the same session only queues behind the first (seen at starts),
  and the bid's session cannot move to a fresh context.
- A run with every value right lost a position in the bid form (72): the website held its answer to one choice for
  about 30 s, three tries of the next click timed out within 14 s, "failed three times" handed the position to the
  model, and the model omitted it. A timeout is a busy page, not a form that cannot be filled: the position is now
  tried again while the page gets half of the time the form had left when it first happened (spent once per form).
- Branch `research/atira-slides`, from the organisers' slides on their own agent. "Forced to be complete": a
  requirement whose ERP findings are not complete (a search without a list, a listed material not read) goes back
  to the ERP up to three times while there is time, instead of costing its positions; mail keys already went back
  (`resolve_references`). "One ledger": model calls keep to one budget of 28 at a time (the platform queues a
  team's calls beyond 32 across all its runs), so a call that waits is not taken for one that does not start.
- From a local run, not the slides: eight mail kinds were read within 118 s and the ninth sat on a tab that hung
  until 275 s. A tab with nothing left to do now runs a search that has taken 1.5 times the median of the finished
  ones, and the first record ends both. A shared page store was measured and dropped: only 2 of 71 mail addresses
  and 5 of 120 ERP addresses were opened by more than one worker.
- Material workers took 4 to 5 steps each again (12 to 19 s) once the pages they had opened stayed in their
  history; with the older batch cut to headings, up to three a run wandered to their step limit.
- Local runs cannot be scored on this machine (`checks/` is not here), and the shared scouting login dropped the
  bid form's session twice in one run, after which the form worker paged back and forth until its 60 steps were
  used. The form has no loop guard.
- Clef does not shorten a step (about 1.0 s a call against 1.3 s for Qwen without thinking, measured again on our
  own page views), but one call answers any number of yes/no questions in that second, and it runs beside Qwen under
  its own limit of 16 calls. So its use is a first pass over a whole result list, not a navigation step.
- The mail worker has a one-step search now (`find_messages`: type, submit, page to the end, all rows back), as the
  ERP worker had; a mail search took about 12 steps against 5 for an ERP search. Clef then marks the rows that are
  the wanted record and those are opened at once; the worker still sees every row and keeps what Clef left out, and
  the reader and the authority rules decide as before. `AGENT_CLEF_ROWS=0` turns the first pass off; `agent.json`
  lists `clef` so that the run waits for it to be warm.
- 11 Oct: the platform takes 64 Qwen calls per team at once (was 32) and 12 runs at a time; the call budget is 60.
  `atira eval` deploys own worlds (up to four sites, a brief, the expected answer) and runs a package on them;
  `atira eval dev` serves one locally and scores each answer. The starter kit is a small world with other names,
  labels and rules than public-1.
- 11 Oct: a mailbox search may combine its words with AND and cut broad searches short, so the reference alone
  lists only part of a folder (one run read no approval and bid without the discount). The survey now runs one
  search per record topic in each folder (the model names field, folder and queries; one tool call runs them all),
  and the model that sorts the rows is asked once about rows with the reference and a record's subject that it left
  out: sorting by date or sender there dropped the version that counted. Which version counts is decided after
  reading, in code. Local run: research done at 173 s instead of 292 s.
- 11 Oct: an action on a page waits at least 0.75 s (0.25 s pause plus network idle); in the sandbox a click on the
  bid site took 4 to 6 s against 0.7 s locally, and a position 12 s. The run summary now ends with the page waits
  per website (`page waits: <site> idle 34x 85.0s; ...`), to see which wait it is before changing one.
- 11 Oct: in the sandbox's browsers a click in a tab behind another tab takes 4 to 6 s instead of 0.7 s (the same
  two clicks took 3.9 s and 5.6 s in three runs and 0.7 s in two others; a bid position 12 s or 4 s). The slow runs
  were the ones where research tabs had been opened in the bid site's window after the form tab; a fresh context
  (own window) and a build that reads over HTTP were fast. Fields and selects were not slower. A tab is now brought
  to the front before a click (`SiteSession.front`). Headless local runs never show this.
- 11 Oct: one message page that hangs held the whole mailbox survey for 75 s (its result is returned when every
  message is read). A message page is now opened again after 15 s, twice at most.
- 11 Oct: what the page waits cost, from the run summary's totals: the browser's "network idle" needs 0.5 s without
  a request and was waited for after every action (ERP tabs 71 to 74 s a run, mail 33 to 39 s, bid form 21 s). A tab
  now counts its own requests and is settled 0.2 s after the last one ended (`SiteSession._idle`; the pause after
  an action is 0.1 s). Same run before and after: specification 45 to 57 s -> 33 to 39 s, all materials read
  92 to 118 s -> 74 to 76 s, a bid position 4 s -> 2.4 s. Note that the browser's signal says nothing after an
  action that loads no new document: it is there at once.
- 11 Oct: one call without thinking wrote for the whole 150 s cap (tokens kept coming, so no pause limit saw it)
  and held the tender research; such calls are now cut at 45 s and retried (`LLM_MAX_CALL_FAST`).
- 11 Oct: the coverage check ran only after all research, so a requirement the ERP had not answered waited for the
  mail (216 s in one eval run); it now runs when the ERP research ends. A material called inactive without a view
  that explains its status is read once more: that guess cost a position in two eval runs.
- 11 Oct: a record kind the mailbox lists nothing for was searched twice at full length (215 s); after a complete
  survey it gets one 60 s search. A topic the first listing showed no row for is listed once more while the other
  messages are read (two kinds missed by the first listing cost 100 s in fallback searches). A message several
  searches list is read once (25 reads became 15), and one whose page gave no record is read again first.
- 11 Oct, public data, quality 100 each: 5:49 -> 3:39 (survey and TAN fixes) -> 3:09 (tab to the front) -> 2:10
  (page waits, second listing pass). Runs in between lost 45 to 150 s to one incident each: a first sign-in that
  hung, the 150 s model call, a fallback mail search.
