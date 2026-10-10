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
- Model calls are queue-bound: across our runs a call waits a median 2 to 3 s for a GPU and then answers in about
  0.35 s (thinking off). Run time is roughly the number of sequential model calls times that wait, and cost is about
  $0.0004 per call, so both fall with fewer, larger steps and with workers whose waits overlap. `dev/dashboard.py`
  shows the split per run (GPU wait against model answering).
- Fastest v1 test run: 27 calls, 87 s, $0.010. The same flow has taken up to 63 calls and 282 s; the spread is the
  TAN round trips and the configurator steps, not the model.
- First four-browser research run (364 calls, 12.8 min, $0.18) still scored 10: research was cut off at its time
  budget without a plan, so the bid fell back to every position omitted. Research must hand over partial findings
  per position before its budget ends, and the budget must leave room for entering positions, not only for signing.
- In that run the TAN lookup hit its 8-request limit twice before the third attempt returned in 4 requests (about
  75 s lost). Not yet checked why; first thing to look at is where the mailbox tab stood when the lookup started.
- run_d4deebd2d3a2d429 (unchanged v1, baseline for branch speed-cost): quality 10, 105 calls, 11.9 min, $0.069,
  against 27 calls and 87 s for the same code earlier. The bid form took 428 s before the first TAN request (model
  answering summed to 933 s, about 9 s per call with thinking on), four of six TAN lookups stopped at the 8-request
  limit, and after the accepted answer the agent kept calling the model and ended in a 401 traceback. v1's time is
  not stable: one run says little, and thinking on in the bid form is the first thing to measure.
