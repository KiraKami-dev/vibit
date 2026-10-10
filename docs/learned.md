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
