# sim: a local mirror of the challenge's websites

Records the four public scouting websites through a browser and serves them again on this machine, so the agent
can be run offline, without the shared scouting login and as often as needed. Nothing here is part of the
submitted package (`agent/`); `atira test-run ./agent --dry-run` lists none of it.

What was recorded is website content. It stays in `sim/captured/` (gitignored) and never goes into the repo, the
package, tests, docs or prompts. The code in this folder holds no wording, markup or values of the websites: the
mirror takes all of that from the recording when it starts. Recording is ordinary signed-in use of the websites
with the scouting logins (pages, searches, the bid form); nothing is guessed at or probed.

## Use

From the repo root:

```sh
# 1. Record (about 30 minutes, once per team member; needs `atira login`)
HEADLESS=1 uv run --project agent python sim/drive.py serve &          # signed-in tabs on the four websites
uv run --project agent python sim/drive.py tender-portal crawl 200    # follow the links a website shows
uv run --project agent python sim/drive.py erp goto "/search?..."     # a search, a form, a gate: one action each
uv run --project agent python sim/drive.py - stop

# 2. Check the mirror against the recording
python3 -m unittest discover -s sim/tests                              # no browser, no model, about 2 s
HEADLESS=1 uv run --project agent python sim/tests/browser_check.py    # the agent's view, mirror vs website

# 3. Run the agent against the mirror (needs `atira models proxy` in another terminal)
dev/run-sim.sh v2 "what changed"
python3 sim/mirror.py                                                  # or just serve it and browse
```

`drive.py` prints each page the way the agent sees it (text with `[eN ...]` refs) and takes `goto`, `click`,
`fill`, `select`, `check`, `back`, `crawl`, `halt`, `label`, `open`. Every action is recorded. The run's login is
replaced by placeholders before anything is written.

## What to record

The mirror can only show what was recorded, so walk what an agent may reach:

- each website's link crawl, after passing its entry checks by hand;
- the specification's revisions and every page of their viewer;
- ERP: each catalogue group in each transaction's search help, then `crawl` from the result list;
- webmail: the tender reference with each record subject the brief's rules name, in each folder, every result
  page and every message; the reference alone and a too-broad word (the "more than 20 hits" page);
- the bid form: both ways through it (bid and decline), each position's choices, a position left out, a position
  added and removed, each mistake once (to record its message), a TAN requested twice, a wrong TAN, a signed
  bid. Sessions on the scouting login end without warning, so script the walk and keep it under a minute.

After an agent run, `sim/captured/_missing.log` lists every address the agent asked for that was not recorded.

## Exact, rebuilt, missing

| | |
| --- | --- |
| Exact | Every recorded page, byte for byte: markup, styles, scripts, nested frames, decoy buttons and popups, hidden text, tooltips, drawn labels. Checked by `test_recorded_pages_come_back_as_recorded` and `browser_check.py`. |
| Rebuilt from what the recording shows | Sign-in, cookie notice, entry checks with a fresh number each time, the error page shown before a page, session timeout (`SIM_SESSION_SECONDS`), the bid form (steps, dependent choices, messages, omitted and added positions, summary, TAN, receipt), TAN mails in the mailbox. The bid form is checked by replaying the recorded walks: every page must equal the one the website answered with. |
| Assumed, not observed | Going back inside the position steps; whether a lead time is required; the form's reaction to a stale revision counter; a refused sign-in. |
| Missing | Any search or address not recorded, dependent choices nobody walked, the ERP's command field. These answer with a "Not recorded" page, never with a guess. |

Things the websites place at random on every page (a popup's close button, the order of some lists and of the
form's positions) are random on the mirror too.

## Limits

- It mirrors the public scenario. The final run uses other software, layouts and data, so a fix that only helps
  here is overfitting: it must stay generic and also hold in a sandbox test run.
- Message counts on the mailbox start page are the recorded ones, also after the mirror handed out new TANs.
- The mirror does not score. `captured/_signed.json` holds the bids signed on it, in the form's own fields.

## Next

Scenarios with invented data in these exact pages (one per trap in the bid rules), an expected answer worked
out independently of `agent/calc.py`, and a scorer with the challenge's weights. The recorded pages then serve as
templates and `sim/captured/` still holds everything that came from the websites.
