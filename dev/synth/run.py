"""
Synthetic scenarios for the agent: generate, check, browse, and run the agent against them with a score.

Each seed is one invented tender on four local websites with their own look (see scenario.py and sites.py). The
expected answer comes from oracle.py, so a run gets the same breakdown a sandbox test run shows, for data and
layouts the agent has never seen. Nothing here is part of the package, and nothing here may flow back into
`agent/` as site knowledge: use a failing seed to find what is too specific in the agent, then fix that in general.

From the repo root (`uv run --project agent` gives the agent's Python and Playwright):
    uv run --project agent python dev/synth/run.py check --seeds 300      # oracle vs agent/calc.py, form round trip
    uv run --project agent python dev/synth/run.py show --seed 7          # features, look and expected answer
    uv run --project agent python dev/synth/run.py serve --seed 7         # browse the four sites yourself
    uv run --project agent python dev/synth/run.py agent --seeds 7,8,9    # needs `atira models proxy` running
    uv run --project agent python dev/synth/run.py export --seeds 0-19    # plain JSON for another environment
Results of `agent` go to experiments/synth-runs.jsonl (gitignored); `report` summarises them by feature.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import oracle  # noqa: E402
import scenario  # noqa: E402
import sites  # noqa: E402

RESULTS = ROOT / "experiments" / "synth-runs.jsonl"
ROLE_TEXT = {
    "tender": "the tender portal: the tender, its bill of quantities{qa} and the bid form where the bid is entered "
              "and signed",
    "supplier": "the buyer's supplier portal: the technical specification and its revisions{qa}",
    "erp": "the company's ERP: materials with status, stock, lead times, price conditions, carton sizes and masses",
    "mail": "the bidder's webmail: the sales director's discount approval, the authority messages the rules name "
            "and the signing TAN",
}


def brief(app: sites.App) -> str:
    w, skin = app.world, app.skin
    access, rules, submission = skin.headings
    lines = []
    for role, site in app.sites.items():
        qa = ", the bidder Q&A (questions and answers)" if skin.qa_site == role else ""
        lines.append(f"- `{site.sid}` ({site.name}): {site.url} — {ROLE_TEXT[role].format(qa=qa)}")
    random.Random(w.seed).shuffle(lines)
    bid_site = app.sites["tender"]
    return f"""# Task brief

You work for {w.company}. Prepare, enter and sign exactly one bid on the public tender **{w.reference}**.

- Tender reference: `{w.reference}`
- Scenario date and time: {w.scenario.strftime('%Y-%m-%dT%H:%M:%SZ')} (the websites run on this clock)
- Bidder: {w.company}, VAT id (USt-IdNr.) {w.vat}
- Sales director, the only person who approves order discounts: {w.director_name} <{w.director}>
- Bidder mailbox: the webmail account of this run. Authorised senders in the rules are a local part plus this
  mailbox's domain.

## {access}

The logins are in the run's access list.

{chr(10).join(lines)}

## {rules}

{(HERE / 'rules.md').read_text(encoding='utf-8').strip()}

## {submission}

Enter the bid in the bid form of `{bid_site.sid}` for tender {w.reference}. Register with the company name and VAT
id above and the bidder mailbox address as contact email. Choose the main offer, or the no-bid declaration when the
decision is no_bid; never an alternative offer. Enter each position with its material number, final quantity in
pieces, net unit price, lead time in weeks and the four breakdown fields, or mark the position as omitted. Enter the
discount percentage and the net total. Acknowledge the tender documents; do not declare your own terms and
conditions. Then request the signing TAN, read it in the webmail and submit the bid with it. A saved bid that is not
signed does not count.
"""


def build_run(app: sites.App, seconds: int = 20 * 60) -> dict:
    access = [{"siteId": s.sid, "url": s.url, "credentials": {"username": s.user, "password": s.password}}
              for s in app.sites.values()]
    random.Random(app.world.seed + 3).shuffle(access)
    bid_site = app.sites["tender"]
    now = int(time.time() * 1000)
    return {"run_id": None, "brief": brief(app), "access": access,
            "submission": {"kind": "site", "siteId": bid_site.sid, "url": bid_site.url},
            "answer_schema": json.loads((HERE / "answer_schema.json").read_text()), "started_at": now,
            "expires_at": now + seconds * 1000}


def judge(app: sites.App, expected: dict) -> dict:
    """The score of the signed bid, with the form's own traps counted (an alternative offer is not a bid)."""
    signed = app.signed
    answer = signed and dict(signed["answer"])
    if answer and signed["meta"]["offer_type"] == "neben":
        answer["decision"] = "alternative_offer"
    result = oracle.score(app.world, expected, answer)
    if signed:
        w = app.world
        meta = signed["meta"]
        result["form"] = {"own_terms_declared": meta["own_terms"], "omitted": meta["omitted"],
                          "extra_rows": meta["extra_rows"], "tan_requests": meta["tan_requests"],
                          "company_ok": meta["company"] == w.company and meta["vat"] == w.vat,
                          "contact_ok": meta["email"].casefold() == w.mailbox.casefold()}
        result["seconds"] = meta["seconds"]
    return result


# ---------------------------------------------------------------- a scripted client, to test the sites themselves


class Client:
    """Fills the bid form over plain HTTP with a given answer: proves a scenario's form and scoring work.
    It knows the form's field names, so it says nothing about the agent."""

    def __init__(self, site: sites.Site):
        self.site = site
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, path: str, form: dict | None = None) -> str:
        data = urlencode(form).encode() if form is not None else None
        for _ in range(3):
            with self.opener.open(self.site.url + path, data, timeout=10) as response:
                text = response.read().decode()
            if "vorübergehend nicht verfügbar" not in text:
                return text
        return text

    def sign_in(self) -> None:
        self.call("/login", {"username": self.site.user, "password": self.site.password, "next": "/"})
        self.call("/consent", {"wahl": "notwendig", "next": "/"})


def enter(app: sites.App, answer: dict) -> None:
    w = app.world
    client = Client(app.sites["tender"])
    client.sign_in()
    base = f"/a/{w.tender.tid}/angebot"
    by_number = {p["position"]: p for p in answer["positions"]}
    values = {"firma": w.company, "ustid": w.vat, "email": w.mailbox, "anerkannt": "1",
              "art": "verzicht" if answer["decision"] == "no_bid" else "haupt",
              "rabatt": scenario.plain(answer["discount_percent"]), "summe": scenario.de(answer["total_net"])}
    for i, p in enumerate(w.tender.positions):
        row = by_number.get(p.number)
        if row is None:
            values[f"p{i}_auslassen"] = "1"
            continue
        values.update({f"p{i}_material": row["material_number"], f"p{i}_menge": str(row["quantity"]),
                       f"p{i}_preis": scenario.de(row["unit_price"]), f"p{i}_lieferzeit": str(row["lead_time_weeks"]),
                       f"p{i}_basis": scenario.de(row["base_unit_price"]),
                       f"p{i}_zuschlag": scenario.de(row["surcharge_per_unit"]),
                       f"p{i}_verpackung": scenario.de(row["packaging_per_unit"]),
                       f"p{i}_bonus": scenario.de(row["rebate_total"])})
    step = 1
    while True:
        page = client.call(f"{base}?schritt={step}")
        names = set(re.findall(r'name="([^"]+)"', page)) - {"aktion", "schritt"}
        form = {n: values[n] for n in names if n in values}
        page = client.call(base, {**form, "schritt": str(step), "aktion": "weiter"})
        if "TAN anfordern" in page or "kann noch nicht" in page:
            break
        step += 1
    client.call(base + "/pruefen", {"aktion": "tan"})
    client.call(base + "/pruefen", {"aktion": "abgeben", "tan": app.tans[w.tender.tid]["tan"]})


def cmd_check(args) -> int:
    bad, decisions = 0, collections.Counter()
    for seed in range(args.first, args.first + args.seeds):
        world = scenario.generate(seed, args.dataset)
        expected = oracle.solve(world)
        decisions[expected["decision"]] += 1
        problems = []
        if oracle.via_calc(world) != expected:
            problems.append("agent/calc.py disagrees with the oracle")
        if seed < args.first + args.forms:
            app = sites.App(world)
            servers = sites.start(app)
            try:
                enter(app, expected)
                result = judge(app, expected)
                if result["quality"] != 100 or not result["eligible"]:
                    problems.append(f"form round trip scored {result['quality']}: {result['wrong'][:3]}")
            except Exception as error:
                problems.append(f"form round trip failed: {error!r}")
            finally:
                for server in servers:
                    server.shutdown()
        if problems:
            bad += 1
            print(f"seed {seed}: {'; '.join(problems)}")
    print(f"{args.seeds} scenarios ({dict(decisions)}), {min(args.forms, args.seeds)} form round trips: "
          f"{bad or 'no'} problem(s)")
    return 1 if bad else 0


def cmd_show(args) -> int:
    world = scenario.generate(args.seed, args.dataset)
    app = sites.App(world)
    print(json.dumps({"seed": args.seed, "features": world.features, "look": app.skin.describe(),
                      "expected": oracle.solve(world, detail=True)}, indent=1, ensure_ascii=False, default=str))
    return 0


def cmd_serve(args) -> int:
    app = sites.App(scenario.generate(args.seed, args.dataset))
    sites.start(app, args.port)
    run = build_run(app, 24 * 3600)
    for site in run["access"]:
        print(f"{site['siteId']:16} {site['url']}  {site['credentials']['username']} / "
              f"{site['credentials']['password']}")
    path = HERE / f".run-{args.seed}.json"
    path.write_text(json.dumps(run))
    print(f"\nThe run (brief, logins) is in {path}. To point the agent at it:\n"
          f"  export CHALLENGE_SCOUTING=\"$(cat {path})\"\nCtrl-C stops the sites.")
    try:
        while app.signed is None:
            time.sleep(1)
        print(json.dumps(judge(app, oracle.solve(app.world)), indent=1, ensure_ascii=False))
    except KeyboardInterrupt:
        pass
    return 0


def export(seed: int, dataset: str = "mixed") -> dict:
    """One scenario as plain data, to load into another environment: what each website shows, the brief, and
    (under "labels" and "expected") what the records mean and the one correct answer, which the sites never show."""
    world = scenario.generate(seed, dataset)
    app = sites.App(world)
    for role, site in app.sites.items():
        site.url = "{{" + role.upper() + "_URL}}"

    def iso(value):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(value, "hour") else value.isoformat()

    def plain(value):
        if isinstance(value, dict):
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(v) for v in value]
        if hasattr(value, "isoformat"):
            return iso(value)
        return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)

    def tender(t) -> dict:
        return {"id": t.tid, "reference": t.reference, "title": t.title, "buyer": t.buyer,
                "positions": [{"position": p.number, "short_text": p.text, "quantity": p.quantity, "unit": p.unit,
                               "requirement": p.requirement} for p in t.positions]}

    def requirement(q) -> dict:
        return {"id": q.rid, "bore_mm": q.bore, "stroke_mm": str(q.stroke), "stroke_is_usable_travel": q.usable,
                "end_reserve_key": q.reserve_key, "pressure_bar": q.pressure, "mounting": q.mounting,
                "certification": q.certification, "catalog_group": q.group, "pieces_per_set": q.pieces_per_set}

    return {
        "generation": scenario.GENERATION, "dataset": dataset, "seed": seed, "features": plain(world.features),
        "scenario": {"tender_reference": world.reference, "scenario_date": iso(world.scenario),
                     "company": world.company, "vat_id": world.vat, "bidder_mailbox": world.mailbox,
                     "mailbox_domain": world.domain, "director": {"name": world.director_name,
                                                                  "address": world.director},
                     "authorised_senders": world.senders()},
        "sites": {role: {"site_id": site.sid, "name": site.name, "url_placeholder": site.url}
                  for role, site in app.sites.items()},
        "brief": brief(app),
        "tender_portal": {
            "tender": tender(world.tender), "other_tenders": [tender(t) for t in world.decoys],
            "qa": [{"number": q.number, "date": iso(q.date), "question": q.question, "answer": q.answer,
                    "labels": {"kind": q.kind, **plain(q.data)}} for q in world.qa]},
        "supplier_portal": {"specification": {
            "title": world.spec_title,
            "revisions": [{"id": r.name, "date": iso(r.date), "status": r.status,
                           "requirements": [requirement(q) for q in r.reqs.values()]} for r in world.revisions]}},
        "erp": {"materials": [{
            "material_number": m.number, "description": m.text, "catalog_group": m.group, "status": m.status,
            "bore_mm": m.bore, "stroke_mm": str(m.stroke), "pressure_bar": m.pressure, "mounting": m.mounting,
            "certification": m.certification, "free_stock": m.free_stock, "blocked_stock": m.blocked_stock,
            "lead_time_stock_weeks": m.lt_stock, "lead_time_production_weeks": m.lt_prod,
            "pieces_per_carton": m.carton, "allocation_mass_kg": str(m.alloc_mass),
            "surcharge_mass_kg": str(m.sur_mass), "metal_index": m.index_code,
            "price_conditions": [{"valid_from": iso(c.valid_from), "valid_to": iso(c.valid_to),
                                  "currency": c.currency, "fx_key": c.fx_key, "price_unit": c.price_unit,
                                  "scales": [{"min_quantity": a, "price": str(b)} for a, b in c.scales]}
                                 for c in m.conditions]} for m in world.materials]},
        "webmail": {"mailbox": world.mailbox, "messages": [{
            "id": m.mid, "folder": m.folder, "from_name": m.sender_name, "from": m.sender, "date": iso(m.date),
            "subject": m.subject, "body": m.body,
            "labels": {"kind": m.kind, "reference": m.reference, "is_record": m.is_record, **plain(m.data)}}
            for m in sorted(world.mails, key=lambda m: m.date)]},
        "expected": oracle.solve(world, detail=True),
    }


def cmd_export(args) -> int:
    folder = args.out / args.dataset
    folder.mkdir(parents=True, exist_ok=True)
    manifest = []
    for seed in args.seeds:
        data = export(seed, args.dataset)
        name = f"{data['features'].get('trap') or 'scenario'}-{seed:04d}.json"
        (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        manifest.append({"file": name, "seed": seed, "tender_reference": data["scenario"]["tender_reference"],
                         "decision": data["expected"]["decision"], "positions": len(
                             data["tender_portal"]["tender"]["positions"]), "features": data["features"]})
    (folder / "manifest.json").write_text(json.dumps({
        "dataset": args.dataset, "what": scenario.DATASETS[args.dataset], "generation": scenario.GENERATION,
        "scenarios": manifest}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{args.dataset}: {len(args.seeds)} scenarios written to {folder}")
    return 0


def run_agent(seed: int, args) -> dict:
    world = scenario.generate(seed, args.dataset)
    expected = oracle.solve(world)
    app = sites.App(world)
    sites.start(app)
    env = {k: v for k, v in os.environ.items() if k not in ("CHALLENGE_RUN_TOKEN", "CHALLENGE_API_URL")}
    traces = ROOT / "agent" / "traces" / f"synth-{seed}-{time.strftime('%Y%m%d-%H%M%S')}"
    traces.mkdir(parents=True, exist_ok=True)
    env.update(CHALLENGE_SCOUTING=json.dumps(build_run(app, args.minutes * 60)), TRACE_DIR=str(traces),
               HEADLESS=env.get("HEADLESS", "1"), NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    env.setdefault("QWEN_BASE_URL", "http://127.0.0.1:8765/qwen/v1")
    env.setdefault("QWEN_API_KEY", "local")
    started = time.time()
    with (traces / "agent.log").open("w") as log:
        try:
            code = subprocess.run(["uv", "run", "--no-sync", "python", "agent.py"], cwd=ROOT / "agent", env=env,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=args.minutes * 60 + 120).returncode
        except subprocess.TimeoutExpired:
            code = -1
    result = judge(app, expected)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "agent"], cwd=ROOT, capture_output=True,
                                text=True).stdout.strip())
    record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seed": seed, "generation": scenario.GENERATION, "version": args.version, "note": args.note,
              "commit": commit.strip(), "dirty": dirty, "quality": result["quality"], "eligible": result["eligible"],
              "decision": expected["decision"], "seconds": result.get("seconds"), "wall": round(time.time() - started),
              "exit": code, "parts": result["parts"], "wrong": result["wrong"], "form": result.get("form"),
              "features": world.features, "look": app.skin.describe(), "requests": app.hits, "traces": str(traces),
              "env": {k: v for k, v in os.environ.items() if re.match(r"AGENT_|LLM_|THINKING|MAX_STEPS|SPEC_", k)}}
    RESULTS.parent.mkdir(exist_ok=True)
    with RESULTS.open("a") as out:
        out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record


def cmd_agent(args) -> int:
    try:
        urllib.request.urlopen(os.environ.get("QWEN_BASE_URL", "http://127.0.0.1:8765/").split("/qwen")[0], timeout=5)
    except urllib.error.HTTPError:
        pass  # the proxy answers, whatever the status
    except OSError:
        print("No model endpoint: start `atira models proxy` in another terminal (or set QWEN_BASE_URL/QWEN_API_KEY).")
        return 1
    for seed in args.seeds:
        record = run_agent(seed, args)
        print(f"seed {seed}: quality {record['quality']} eligible {record['eligible']} "
              f"({record['decision']} case) in {record['seconds'] or '-'} s; wrong: {record['wrong'][:8]}", flush=True)
    return 0


def cmd_report(args) -> int:
    if not RESULTS.exists():
        print("No synthetic runs recorded yet.")
        return 0
    runs = [json.loads(line) for line in RESULTS.read_text().splitlines() if line.strip()]
    if args.version:
        runs = [r for r in runs if r.get("version") == args.version]
    print(f"{len(runs)} runs, mean quality {sum(r['quality'] for r in runs) / max(1, len(runs)):.1f}, "
          f"{sum(r['quality'] == 100 for r in runs)} at 100, {sum(not r['eligible'] for r in runs)} not eligible")
    by: dict[str, list[float]] = collections.defaultdict(list)
    for r in runs:
        for source in ("features", "look"):
            for key, value in r[source].items():
                if isinstance(value, (bool, str)):
                    by[f"{source}.{key}={value}"].append(r["quality"])
    for key, scores in sorted(by.items(), key=lambda kv: sum(kv[1]) / len(kv[1])):
        print(f"  {sum(scores) / len(scores):6.1f}  n={len(scores):<3} {key}")
    misses = collections.Counter(re.sub(r"position \S+: ", "position: ", w) for r in runs for w in r["wrong"])
    print("most often wrong:", misses.most_common(10))
    return 0


def seed_list(text: str) -> list[int]:
    seeds: list[int] = []
    for part in text.split(","):
        first, _, last = part.partition("-")
        seeds += range(int(first), int(last or first) + 1)
    return seeds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--seeds", type=int, default=300)
    check.add_argument("--first", type=int, default=0)
    check.add_argument("--forms", type=int, default=60, help="how many of the seeds also get a form round trip")
    show = commands.add_parser("show")
    show.add_argument("--seed", type=int, required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--seed", type=int, required=True)
    serve.add_argument("--port", type=int, default=8800)
    agent = commands.add_parser("agent")
    agent.add_argument("--seeds", type=seed_list, required=True, help="e.g. 0-5 or 3,7,12")
    agent.add_argument("--minutes", type=int, default=20)
    agent.add_argument("--version", default="")
    agent.add_argument("--note", default="")
    out = commands.add_parser("export")
    out.add_argument("--seeds", type=seed_list, required=True, help="e.g. 0-19 or 3,7,12")
    out.add_argument("--out", type=Path, default=HERE / "data")
    report = commands.add_parser("report")
    report.add_argument("--version", default="")
    for command in (check, show, serve, agent, out):
        command.add_argument("--dataset", default="mixed", choices=list(scenario.DATASETS))
    args = parser.parse_args()
    return {"check": cmd_check, "show": cmd_show, "serve": cmd_serve, "agent": cmd_agent, "export": cmd_export,
            "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
