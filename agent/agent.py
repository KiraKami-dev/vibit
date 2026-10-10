"""
The agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

v2: read the brief, then the orchestrator (planner, findings board, research workers, verifier, calc) builds the
answer, and submit.py enters and signs the bid with it; without an answer every position is omitted, as in v1.
AGENT_RESEARCH=0 skips the research (the v1 path). See docs/plan.md.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import localenv  # noqa: F401  (first: local runs read agent/.env before any module reads its settings)
import browser_tools
import browsers
import loopwatch
import run as contract
from briefing import read_brief
from browser_tools import SiteSession
from orchestrator import Orchestrator
from playwright.async_api import async_playwright
from submit import BidFlow, Mailbox, submit_bid
import tracing
from tracing import Trace

SAFETY_SECONDS = 90  # the answer must be in before the run expires
LOCAL_SECONDS = 20 * 60  # the scouting run does not expire; behave like a scored run
RESEARCH_START_SECONDS = 600  # a website that only research needs is warmed this long, then given up


def warm_up(sessions: dict[str, SiteSession], deadline: float, critical: set[str]) -> dict[str, asyncio.Task]:
    """
    Start signing in to every website in the background and return at once: nothing waits for all of them. The
    `critical` ones (the bid website, the mailbox) keep retrying until START_MARGIN before the deadline; the others
    give up after RESEARCH_START_SECONDS. A session whose start is still running can be handed out: its first look
    waits for the start (SiteSession.revive).
    """
    browser_tools.DEADLINE = deadline
    tasks = {}
    for name, session in sessions.items():
        limit = deadline - browser_tools.START_MARGIN
        if name not in critical:
            limit = min(limit, time.monotonic() + RESEARCH_START_SECONDS)
        session.until = limit
        tasks[name] = asyncio.create_task(session.start(until=limit), name=f"start@{name}")
        tasks[name].add_done_callback(lambda t: t.cancelled() or t.exception())  # failures are traced in start
    return tasks


async def solve(task: dict, page, answer: dict | None = None,
                attributes: dict[str, dict[str, str]] | None = None) -> dict | None:
    """
    Enter the bid on the run's website (a "site" submission) and return None.

    `answer` (calc's BidResult.answer()) and `attributes` (per position, the chosen material's attributes) are what
    the bid worker enters; positions they do not give completely are omitted. Unless given, the orchestrator
    researches them; a research failure never costs the signed bid.
    """
    trace = Trace()
    loopwatch.Watchdog(activity=lambda: trace.last).start()  # a blocked loop or a silent run dumps its stacks
    left = min(contract.seconds_left(task), LOCAL_SECONDS)
    deadline = time.monotonic() + left - SAFETY_SECONDS
    # Sign in to every website in the background: the brief, the planner and the workers never wait for all of
    # them. The bid website (named by the run contract) keeps warming; the mailbox too, once the brief names it.
    contexts = await browsers.more_contexts(page, len(task["access"]))
    sessions = {site["siteId"]: SiteSession(site, context, trace) for site, context in zip(task["access"], contexts)}
    contract_site = (task.get("submission") or {}).get("siteId")
    starts = warm_up(sessions, deadline, {contract_site} if contract_site in sessions else set(sessions))
    briefing = await read_brief(task)
    # The run contract names the submission website; it wins over the model's reading of the brief.
    if contract_site in sessions and contract_site != briefing.bid_site:
        trace.event("briefing", "bid_site_corrected", read=briefing.bid_site, contract=contract_site)
        briefing = briefing.model_copy(update={"bid_site": contract_site})
    trace.event("briefing", "done", **briefing.model_dump(exclude={"submission_steps"}))
    bid, mail = sessions[briefing.bid_site], sessions[briefing.mail_site]
    for session in (bid, mail):  # critical after all: warm until the margin before the deadline
        session.until = max(session.until or 0, deadline - browser_tools.START_MARGIN)
    # The bid form's steps that need no research (bidder data, offer type) run while the research does.
    flow = BidFlow(bid, Mailbox(mail, briefing, deadline), briefing, deadline)
    flow.start()
    if answer is None and os.environ.get("AGENT_RESEARCH", "1") != "0":
        answer, attributes = await research(task, briefing, contexts, trace, deadline, sessions)
    # A start still running is waited for by the session's first look; one that gave up gets a fresh tab there.
    trace.event("starts", "state", **{name: ("up" if s.ok else "starting" if s.starting else "down")
                                      for name, s in sessions.items()}, pending=sum(not t.done() for t in starts.values()))
    try:
        result = await flow.finish(answer, attributes)
    except Exception as error:  # never end without trying to sign: one more pass with what is on screen
        trace.event("submit", "crashed", error=f"{type(error).__name__}: {str(error)[:300]}")
        result = await submit_bid(bid, Mailbox(mail, briefing, deadline), briefing, deadline, answer, attributes)
    trace.event("result", "done", result=result.model_dump() if result else None)
    return None


async def research(task: dict, briefing, contexts: list, trace: Trace, deadline: float,
                   warm: dict[str, SiteSession] | None = None) -> tuple[dict | None, dict[str, dict[str, str]] | None]:
    """The orchestrator's answer and material attributes; (None, None) when it fails or builds nothing."""
    answer, attributes, problems = None, None, []
    orchestrator = None
    started = time.monotonic()
    try:
        orchestrator = Orchestrator(task, briefing, {s["siteId"]: c for s, c in zip(task["access"], contexts)},
                                    trace, deadline, warm=warm)
        problems = orchestrator.board.problems
        answer, _ = await orchestrator.run()
        attributes = orchestrator.attributes
    except Exception as error:  # the signed bid must never be lost to a research failure
        trace.event("orchestrator", "failed", error=f"{type(error).__name__}: {str(error)[:300]}")
    if answer is None and orchestrator is not None:  # still the reference, decision and any discount read
        answer = orchestrator.fallback_answer()
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
        try:
            answer = await solve(task, page)
        finally:  # the platform keeps only the last lines of a run's output: repeat the phases there
            tracing.print_summary()
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
