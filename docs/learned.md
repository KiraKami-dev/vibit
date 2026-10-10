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
