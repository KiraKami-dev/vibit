"""
v0 check: one worker per website, each signs in and reads one page it chooses, in parallel.

Run from the repo root with the scouting logins and the models proxy:
    eval "$(atira scout --env)"; HEADLESS=1 uv run --project agent python dev/smoke.py [siteId ...]
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import browsers  # noqa: E402
import run as contract  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from tracing import Trace  # noqa: E402
from worker import run_worker  # noqa: E402


class PageReport(BaseModel):
    page_title: str
    url: str
    summary: str
    items: list[str]


GOALS = {
    "tender-portal": "Open the page of the tender named in the context and list its documents and positions.",
    "customer-portal": "Find the buyer's technical specification documents and list them with their revisions.",
    "erp": "Find out which transactions the ERP offers and list them with what each one is for.",
    "mail": "Open the inbox and list the subjects and senders of the newest five messages.",
}


async def main() -> None:
    task, _ = contract.load_run()
    sites = [s for s in task["access"] if len(sys.argv) < 2 or s["siteId"] in sys.argv[1:]]
    trace = Trace()
    context = task["brief"].split("## Rules")[0].strip()
    async with async_playwright() as playwright:
        ctxs = await browsers.contexts(playwright, len(sites))
        sessions = [SiteSession(site, ctx, trace) for site, ctx in zip(sites, ctxs)]
        await asyncio.gather(*(s.start() for s in sessions))
        deadline = time.monotonic() + float(__import__("os").environ.get("SMOKE_SECONDS", "600"))
        results = await asyncio.gather(*(run_worker(s, GOALS[s.name], PageReport, context=context,
                                                    deadline=deadline, max_steps=25,
                                                    thinking=os.environ.get("THINKING") == "1") for s in sessions))
    for session, (report, notes) in zip(sessions, results):
        print(f"\n===== {session.name}")
        print(report.model_dump_json(indent=1) if report else f"no result; notes: {notes}")
    print(f"\ntrace: {trace.path}")


if __name__ == "__main__":
    asyncio.run(main())
