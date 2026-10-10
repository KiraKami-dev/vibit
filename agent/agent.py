"""
The agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

v2: read the brief, then the orchestrator (planner, findings board, research workers, verifier, calc) builds the
answer, and submit.py enters and signs the bid with it; without an answer every position is omitted, as in v1.
AGENT_RESEARCH=0 skips the research (the v1 path). See docs/plan.md.

v3: the specification is found by three racing attempts with different strategies, workers that go in
circles are warned and (in the race) stopped, the bid form gets more time, and a bid without positions
still carries the discount. See docs/changes-v3.md for the changes and the environment variables.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import browsers
import run as contract
from briefing import read_brief
from browser_tools import SiteSession
from orchestrator import Orchestrator
from playwright.async_api import async_playwright
from submit import Mailbox, submit_bid
from tracing import Trace

SAFETY_SECONDS = 90  # the answer must be in before the run expires
LOCAL_SECONDS = 20 * 60  # the scouting run does not expire; behave like a scored run


async def solve(task: dict, page, answer: dict | None = None,
                attributes: dict[str, dict[str, str]] | None = None) -> dict | None:
    """
    Enter the bid on the run's website (a "site" submission) and return None.

    `answer` (calc's BidResult.answer()) and `attributes` (per position, the chosen material's attributes) are what
    the bid worker enters; positions they do not give completely are omitted. Unless given, the orchestrator
    researches them; a research failure never costs the signed bid.
    """
    trace = Trace()
    left = min(contract.seconds_left(task), LOCAL_SECONDS)
    deadline = time.monotonic() + left - SAFETY_SECONDS
    # Sign in to every website while the model reads the brief: which one plays which part is only known after it.
    contexts = await browsers.more_contexts(page, len(task["access"]))
    sessions = {site["siteId"]: SiteSession(site, context, trace) for site, context in zip(task["access"], contexts)}
    started = asyncio.gather(*(session.start() for session in sessions.values()), return_exceptions=True)
    briefing, outcomes = await asyncio.gather(read_brief(task), started)
    # The run contract names the submission website; it wins over the model's reading of the brief.
    contract_site = (task.get("submission") or {}).get("siteId")
    if contract_site in sessions and contract_site != briefing.bid_site:
        trace.event("briefing", "bid_site_corrected", read=briefing.bid_site, contract=contract_site)
        briefing = briefing.model_copy(update={"bid_site": contract_site})
    trace.event("briefing", "done", **briefing.model_dump(exclude={"submission_steps"}))
    failed = {name for name, outcome in zip(sessions, outcomes) if isinstance(outcome, BaseException)}
    bid, mail = sessions[briefing.bid_site], sessions[briefing.mail_site]
    await asyncio.gather(*(session.start() for session in (bid, mail) if session.name in failed),
                         return_exceptions=True)  # submit_bid copes with a session that is still down
    if answer is None and os.environ.get("AGENT_RESEARCH", "1") != "0":
        answer, attributes = await research(task, briefing, contexts, trace, deadline)
    result = await submit_bid(bid, Mailbox(mail, briefing, deadline), briefing, deadline, answer, attributes)
    trace.event("result", "done", result=result.model_dump() if result else None)
    return None


async def research(task: dict, briefing, contexts: list, trace: Trace, deadline: float
                   ) -> tuple[dict | None, dict[str, dict[str, str]] | None]:
    """The orchestrator's answer and material attributes; (None, None) when it fails or builds nothing."""
    answer, attributes, problems = None, None, []
    started = time.monotonic()
    try:
        orchestrator = Orchestrator(task, briefing, {s["siteId"]: c for s, c in zip(task["access"], contexts)},
                                    trace, deadline)
        problems = orchestrator.board.problems
        answer, _ = await orchestrator.run()
        attributes = orchestrator.attributes
    except Exception as error:  # the signed bid must never be lost to a research failure
        trace.event("orchestrator", "failed", error=f"{type(error).__name__}: {str(error)[:300]}")
    trace.event("orchestrator", "done", seconds=round(time.monotonic() - started, 1), answer=answer,
                attributes=attributes, problems=problems[:30])
    path = Path(trace.path).with_name("answer.json")
    path.write_text(json.dumps(answer, ensure_ascii=False, indent=1))
    print(f"Answer for the bid form written to {path}", flush=True)
    return answer, attributes


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
