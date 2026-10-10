"""
Research check: run the research workers against the scouting websites and print what they established.

Run from the repo root with the scouting logins and the models proxy:
    eval "$(atira scout --env)"; HEADLESS=1 uv run --project agent python dev/research_check.py [part ...]
Parts: tender, spec, mail, erp (erp needs tender and spec; no part means all). RESEARCH_SECONDS sets the deadline
(default 360); TRACE_DIR the trace directory (default agent/traces).
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

AGENT = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(AGENT))
os.environ.setdefault("TRACE_DIR", str(AGENT / "traces"))

import browsers  # noqa: E402
import llm  # noqa: E402
import run as contract  # noqa: E402
from briefing import read_brief  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from research import PARTS, research  # noqa: E402
from tracing import Trace  # noqa: E402

ROLES = ("tender_site", "spec_site", "erp_site", "mail_site")


class SiteRoles(BaseModel):
    tender_site: str = Field(description="siteId of the website with the tender and its bidder Q&A")
    spec_site: str = Field(description="siteId of the website with the buyer's technical specification")
    erp_site: str = Field(description="siteId of the company's ERP with materials, stock and prices")
    mail_site: str = Field(description="siteId of the bidder's webmail")


async def site_roles(task: dict, briefing) -> SimpleNamespace:
    """The briefing with a siteId per role; while `Briefing` lacks one, a model maps it from the brief (here only)."""
    known = {role: getattr(briefing, role, "") for role in ROLES}
    if not all(known.values()):
        sites = "\n".join(f"- siteId {s['siteId']}: {s['url']}" for s in task["access"])
        agent = Agent(llm.model(), output_type=SiteRoles, model_settings=llm.settings(thinking=True), retries=3,
                      instructions="You read a task brief and say which website plays which part.")
        mapped = (await agent.run(f"Websites of this run:\n{sites}\n\nBrief:\n{task['brief']}")).output
        known = {role: known[role] or getattr(mapped, role) for role in ROLES}
    return SimpleNamespace(**{**briefing.model_dump(), **known})


async def main() -> None:
    parts = tuple(sys.argv[1:]) or PARTS
    if "erp" in parts:
        parts = tuple(dict.fromkeys(("tender", "spec", *parts)))
    task, _ = contract.load_run()
    trace = Trace()
    briefing = await site_roles(task, await read_brief(task))
    sites = {site["siteId"]: site for site in task["access"]}
    sessions: list[SiteSession] = []
    started = time.monotonic()
    async with async_playwright() as playwright:
        used = list(dict.fromkeys(getattr(briefing, role) for role in ROLES))
        contexts = dict(zip(used, await browsers.contexts(playwright, len(used))))

        async def open_session(site_id: str) -> SiteSession:
            session = SiteSession(sites[site_id], contexts[site_id], trace)
            sessions.append(session)
            await session.start()
            return session

        updates = []
        deadline = time.monotonic() + float(os.environ.get("RESEARCH_SECONDS", "360"))
        findings = await research(briefing, open_session, deadline, trace,
                                  on_update=lambda found: updates.append(round(time.monotonic() - started, 1)),
                                  parts=parts)
    seconds = time.monotonic() - started
    print("\n===== findings")
    print(findings.model_dump_json(indent=1))
    print("\n===== workers")
    total = 0
    for session in sessions:
        for run in session.runs:
            total += run["requests"]
            print(f"{run['worker']:<22} {run['outcome']:<22} {run['requests']:>3} requests {run['seconds']:>6.1f} s  "
                  f"{run['input_tokens']:>7} in {run['output_tokens']:>6} out  {run['goal']}")
    print(f"\ntotal: {total} requests, {seconds:.1f} s, {len(sessions)} tabs, {len(updates)} updates "
          f"(first at {updates[0] if updates else '-'} s)")
    print(f"trace: {trace.path}")


if __name__ == "__main__":
    asyncio.run(main())
