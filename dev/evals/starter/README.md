# Eval starter kit

A working eval: four basic sites like the challenge's, a brief and the expected bid. They're deliberately far too
simple. Make yours harder, then deploy.

It has no dependencies (plain Node 24, so `atira-eval.json` needs no `build`) and holds a small RFQ world, each fact on
the site where the main challenge keeps it:

- `tender-portal`, a tender portal: the tender VG-2026-0142 with its Leistungsverzeichnis (three positions, each with
  a quantity and a requirement reference), "Teilnahme bekunden" and the bid configurator ("Angebotsabgabe"), whose
  steps and labels follow the main challenge's tender portal;
- `customer-portal`, the buyer's supplier portal: the technical specification with each requirement's bore, stroke,
  mounting and certification, in a current and a superseded revision;
- `erp`, the bidder's ERP: materials with their attributes, status, stock, lead times and price scales;
- `mail`, the bidder's mailbox: the buyer's request (it changes one quantity), the discount approval, an approval for
  another tender, and the signing TANs the tender portal sends.

`expected.json` is the one correct bid for this world, following the rules in `BRIEF.md`.

## Try it

```sh
atira eval dev .          # the four sites on 127.0.0.1:8710–8713 with a login each; each answer is scored and printed
atira eval deploy .       # upload it; prints the world's URLs and preview logins
```

Then open the tender portal, sign in with the printed login, bid, and see the score in the terminal. To run an agent against it, in another terminal:
`eval "$(atira eval dev --env .)"` exports the run as `CHALLENGE_SCOUTING`, as `atira scout --env` does.

## What to change

| File | What |
| --- | --- |
| `world.js` | The data: tender, positions, the specification's requirements, mails, materials and prices |
| `BRIEF.md` | The task and its rules, as the agent reads them |
| `expected.json` | The correct answer for your world: change it whenever the world or the rules change |
| `sites/*.js` | The pages. `sites/tender-portal.js` holds the configurator and hands the answer in |
| `atira-kit.js` | Login, session and answer through the platform's endpoints: copy it as it is |
| `atira-eval.json` | How to start the app and its sites (1–4, each a name and a port) |

The rules the sites must keep:

- Listen on `PORT_<NAME>` (`sites/html.js` reads it, with the manifest's port as the default).
- Link to another site through `ATIRA_SITE_URL_<NAME>`, the address the agent's browser uses (localhost when unset).
- Need a login on every page and hand the answer in from the app: `atira-kit.js` does both. Its `guard` serves
  `/login` (the main challenge's field names) and `/logout`, checks each login with the platform
  (`POST $ATIRA_API_URL/v1/eval-worlds/verify`) and keeps a signed cookie per site; `submitAnswer` hands a run's bid to
  `POST $ATIRA_API_URL/v1/eval-worlds/answer`: `400` with `issues` for a bid that doesn't match the answer schema,
  `202` for a run (the run ends), `200` with the score for a preview. Copy it into your own app.
- Keep each run's state apart: one app serves every run of a version (`sites/html.js` `perRun` keys it by the login).
  The app reaches only the platform's API.

## Scoring

The main challenge's scorer, against `expected.json`: the decision must match; the tender reference scores 5, the
discount 5, the net total 15 and the positions 75, shared equally (material 35 %, quantity 20 %, unit price 30 %,
lead time 15 % of a position's share); a line for a position that isn't in `expected.json` costs one share. The guide:
`docs/evals.md` on the platform.
