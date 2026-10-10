"""
The agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

v1: read the brief, then sign and submit a bid with every position omitted (eligible, decision and reference
points). Workers for the specification, ERP and pricing come in v2+. See docs/plan.md.
"""
import asyncio
import json
import sys
import time

import browsers
import run as contract
from briefing import read_brief
from browser_tools import SiteSession
from playwright.async_api import async_playwright
from submit import Mailbox, submit_bid
from tracing import Trace

SAFETY_SECONDS = 90  # the answer must be in before the run expires
LOCAL_SECONDS = 20 * 60  # the scouting run does not expire; behave like a scored run


async def solve(task: dict, page) -> dict | None:
    """Enter the bid on the run's website (a "site" submission) and return None."""
    trace = Trace()
    left = min(contract.seconds_left(task), LOCAL_SECONDS)
    deadline = time.monotonic() + left - SAFETY_SECONDS
    # Sign in to every website while the model reads the brief: which one plays which part is only known after it.
    contexts = await browsers.more_contexts(page, len(task["access"]))
    sessions = {site["siteId"]: SiteSession(site, context, trace) for site, context in zip(task["access"], contexts)}
    started = asyncio.gather(*(session.start() for session in sessions.values()), return_exceptions=True)
    briefing, outcomes = await asyncio.gather(read_brief(task), started)
    trace.event("briefing", "done", **briefing.model_dump(exclude={"submission_steps"}))
    failed = {name for name, outcome in zip(sessions, outcomes) if isinstance(outcome, BaseException)}
    bid, mail = sessions[briefing.bid_site], sessions[briefing.mail_site]
    await asyncio.gather(*(session.start() for session in (bid, mail) if session.name in failed))
    result = await submit_bid(bid, Mailbox(mail, briefing, deadline), briefing, deadline)
    trace.event("result", "done", result=result.model_dump() if result else None)
    return None


async def main() -> int:
    task, token = contract.load_run()
    sites = ", ".join(f"{site.get('siteId')} {site.get('url')}" for site in task["access"])
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    async with async_playwright() as playwright:
        browser, page = await browsers.connect(playwright)  # more browsers: browsers.connect(playwright, 1), ...
        answer = await solve(task, page)
    if answer is None:
        print("No answer to submit through the API: the bid is entered on the website", flush=True)
        return 0
    if not token:
        print("Your answer (locally nothing is submitted):", json.dumps(answer, ensure_ascii=False), flush=True)
        return 0
    status, body = contract.submit_answer(task, token, answer)
    print(f"Answer: HTTP {status} {body[:2000]}", flush=True)
    return 0 if status == 202 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
