"""
Check the tender and mail research workers: read the brief, then research the bid website's tender and Q&A and the
bidder's webmail in parallel, and print the records, the calc mapping and the timings. Read-only: nothing is
entered on the bid form, no TAN is requested.

Run from the repo root with the scouting logins and the models proxy:
    eval "$(atira scout --env)"; HEADLESS=1 uv run --project agent python dev/research_check_records.py [tender|mail]
MAIL_TABS (default 5) sets how many webmail tabs share the record kinds.
"""
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import browsers  # noqa: E402
import run as contract  # noqa: E402
from briefing import read_brief  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from research_mail import bid_clauses, plan_searches, research_mail  # noqa: E402
from research_tender import research_tender  # noqa: E402
from tracing import Trace  # noqa: E402


def scenario_time(text: str) -> datetime:
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def dump(title: str, model) -> None:
    print(f"\n===== {title}")
    print(json.dumps(model.model_dump(mode="json"), indent=1, ensure_ascii=False) if model is not None else "None")


async def main() -> None:
    parts = set(sys.argv[1:]) or {"tender", "mail"}
    task, _ = contract.load_run()
    trace = Trace()
    t0 = time.monotonic()
    briefing = await read_brief(task)
    t_brief = time.monotonic() - t0
    print(f"brief: {briefing.tender_reference} {briefing.scenario_date} bid={briefing.bid_site} "
          f"mail={briefing.mail_site} ({t_brief:.1f}s)")
    scenario = scenario_time(briefing.scenario_date)
    sites = {s["siteId"]: s for s in task["access"]}
    deadline = time.monotonic() + float(os.environ.get("CHECK_SECONDS", "600"))
    tabs = int(os.environ.get("MAIL_TABS", "5"))
    async with async_playwright() as playwright:
        bid_ctx, mail_ctx = await browsers.contexts(playwright, 2)
        timings: dict[str, float] = {}

        async def tender():
            started = time.monotonic()
            session = SiteSession(sites[briefing.bid_site], bid_ctx, trace)
            await asyncio.wait_for(session.start(), 90)
            result = await research_tender(session, briefing.tender_reference, scenario, deadline=deadline)
            timings["tender"] = time.monotonic() - started
            return result

        # Planning overlaps the tabs' sign-in.
        plan = asyncio.create_task(plan_searches(task["brief"], {k: v["url"] for k, v in sites.items()}))

        async def mail():
            started = time.monotonic()
            first = SiteSession(sites[briefing.mail_site], mail_ctx, trace)
            await first.start()  # signs in once; the other tabs share the context's cookies
            others = [SiteSession(sites[briefing.mail_site], mail_ctx, trace) for _ in range(tabs - 1)]
            await asyncio.gather(*(s.start() for s in others))
            kinds = await plan
            timings["mail_plan_ready"] = time.monotonic() - started
            result = await research_mail([first, *others], briefing.tender_reference, scenario, task["brief"],
                                         kinds=kinds, deadline=deadline)
            timings["mail"] = time.monotonic() - started
            return result

        jobs = {"tender": tender, "mail": mail}
        names = [n for n in ("tender", "mail") if n in parts]
        done = await asyncio.gather(*(jobs[n]() for n in names), return_exceptions=True)
        for name, value in zip(names, done):
            if isinstance(value, BaseException):
                print(f"{name} failed: {type(value).__name__}: {value}")
        results = {n: v for n, v in zip(names, done) if not isinstance(v, BaseException)}
    total = time.monotonic() - t0

    tender_facts, mail_facts = results.get("tender"), results.get("mail")
    if tender_facts is not None:
        dump("tender: Q&A records", tender_facts.qa)
        print("\n===== tender: positions")
        for p in tender_facts.positions:
            print(f"  {p.position} {p.short_text!r} req={p.requirement} {p.tender_quantity}->{p.quantity} "
                  f"{p.unit_text} (answer {p.quantity_answer}) group={p.condition_group} pack={p.packaging_key} "
                  f"term={p.commitment_key} pool={p.setup_pool_key} bonus={p.bonus_key} setup={p.setup_fee}")
        print(f"  withdrawn: {tender_facts.withdrawn}")
        for a in tender_facts.amendments:
            print(f"  amendment {a.date.isoformat()} {a.revision} {a.requirement} {a.attributes}")
    if mail_facts is not None:
        print("\n===== mail: sources")
        for s in sorted(mail_facts.sources, key=lambda s: (s.kind, s.sent_at)):
            flag = "USED" if s.used else ("rec " if s.is_record and s.has_content else "-   ")
            print(f"  {flag} {s.kind:<15} {s.sent_at.isoformat()} {s.sender:<36} "
                  f"{s.reference:<18} {s.subject}")
        dump("mail: facts", mail_facts.model_copy(update={"sources": []}))
        if mail_facts.problems:
            print("mail problems:", *mail_facts.problems, sep="\n  ")
    if tender_facts is not None and mail_facts is not None:
        dump("calc mapping", bid_clauses(tender_facts, mail_facts))
    print(f"\ntimings: brief {t_brief:.1f}s, " + ", ".join(f"{k} {v:.1f}s" for k, v in timings.items())
          + f", total {total:.1f}s")
    print(f"trace: {trace.path}")


if __name__ == "__main__":
    asyncio.run(main())
