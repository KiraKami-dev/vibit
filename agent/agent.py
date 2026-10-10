"""
The agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

Read the brief, let the research workers collect findings on the run's websites (one browser context per website,
any number of tabs in it), turn the findings into a plan, then enter, sign and submit that plan in the bid form.
Whatever research or planning does not deliver in time is left out of the bid: the run always ends with a signed
bid. See docs/plan.md.
"""
import asyncio
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import browsers
import run as contract
from bidplan import BidPlan, PlanEntry, build_plan
from briefing import Briefing, read_brief
from browser_tools import SiteSession
from evidence import Findings
from playwright.async_api import async_playwright
from submit import Mailbox, close_tab, enterable, submit_bid
from tracing import Trace

LOCAL_SECONDS = 20 * 60  # the scouting run does not expire; behave like a scored run
# The run's time budget. Each mark is a share of the time the run has, with a ceiling in seconds.
RESEARCH_SHARE, RESEARCH_MAX_SECONDS = 0.45, 7 * 60  # research hands over its findings by this mark
ENTRY_SHARE, ENTRY_MAX_SECONDS = 0.50, 7.5 * 60  # bid entry starts by this mark, with the findings there are
MIN_ENTRY_SECONDS = 150  # entering and signing never get less than this, however short the run
SAFETY_SECONDS = 90  # the bid must be signed this long before the run expires


@dataclass
class Tab(SiteSession):
    """A website tab with a name of its own, so tabs on one website stay apart in the trace."""

    label: str = ""

    @property
    def name(self) -> str:
        return self.label or self.site["siteId"]


def marks(started: float, seconds: float) -> tuple[float, float, float]:
    """The run's marks as `time.monotonic()` values: research ends, entry starts at the latest, signed by."""
    sign_by = started + seconds - SAFETY_SECONDS
    entry_by = min(started + min(seconds * ENTRY_SHARE, ENTRY_MAX_SECONDS), sign_by - MIN_ENTRY_SECONDS)
    research_by = min(started + min(seconds * RESEARCH_SHARE, RESEARCH_MAX_SECONDS), entry_by)
    return research_by, entry_by, sign_by


def scenario_time(text: str) -> datetime:
    """The scenario date as the brief states it, timezone-aware. A time without a zone is read in the zone the text
    names (e.g. Europe/Berlin), else as UTC; a date without a time is the start of that day."""
    iso = re.search(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)?\s*(?:Z|[+-]\d{2}:?\d{2})?", text)
    day_first = re.search(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})(?:\D{1,6}(\d{1,2})[:.](\d{2}))?", text)
    if iso:
        found = datetime.fromisoformat(re.sub(r"\s+(?=[Z+-])", "", iso.group().strip()))
    elif day_first:
        day, month, year, hour, minute = (int(part or 0) for part in day_first.groups())
        found = datetime(year, month, day, hour, minute)
    else:
        raise ValueError(f"no date in {text!r}")
    if found.tzinfo is None:
        zone = re.search(r"\b[A-Z][A-Za-z_]+/[A-Z][A-Za-z_]+\b", text)
        try:
            found = found.replace(tzinfo=ZoneInfo(zone.group()) if zone else timezone.utc)
        except Exception:
            found = found.replace(tzinfo=timezone.utc)
    return found


def fallback_plan(tender_reference: str, findings: Findings | None, reason: str) -> BidPlan:
    """A bid with every position left out: what is signed when research or planning did not deliver."""
    positions = [position.position for position in findings.tender.positions] if findings else []
    return BidPlan(tender_reference=tender_reference,
                   entries=[PlanEntry(position=position, omit=True, reason=reason) for position in positions],
                   notes=[f"every position omitted: {reason}"])


def make_plan(findings: Findings | None, briefing: Briefing, mailbox_address: str) -> BidPlan:
    """The plan the findings support; any failure on the way ends in the plan with every position left out."""
    if findings is None or findings == Findings():
        return fallback_plan(briefing.tender_reference, None, "research returned no findings")
    try:
        plan = build_plan(findings, briefing.tender_reference, scenario_time(briefing.scenario_date),
                          mailbox_address.rpartition("@")[2].strip().lower())
        if plan.tender_reference != briefing.tender_reference:  # the brief names the tender, nothing else does
            plan = plan.model_copy(update={"tender_reference": briefing.tender_reference})
        return plan
    except Exception as error:  # includes NotImplementedError while planning is being built
        return fallback_plan(briefing.tender_reference, findings, f"planning failed ({type(error).__name__}: "
                                                                  f"{str(error)[:120]})")


async def gather_findings(briefing: Briefing, open_session, research_by: float, entry_by: float, trace: Trace
                          ) -> Findings | None:
    """Run the research until its mark; at the entry mark at the latest, go on with what it has handed over."""
    latest: list[Findings | None] = [None]

    def on_update(findings: Findings) -> None:
        latest[0] = findings

    try:
        from research import research  # imported here: a missing or broken module must not stop the bid
        found = await asyncio.wait_for(research(briefing, open_session, research_by, trace, on_update),
                                       max(1.0, entry_by - time.monotonic()))
        return found if isinstance(found, Findings) else latest[0]
    except Exception as error:  # out of time, no research module yet, or a failure inside it
        trace.event("research", "stopped", reason=f"{type(error).__name__}: {str(error)[:200]}",
                    partial=latest[0] is not None)
        return latest[0]


async def solve(task: dict, page) -> dict | None:
    """Enter the bid on the run's website (a "site" submission) and return None."""
    trace = Trace()
    started = time.monotonic()
    research_by, entry_by, sign_by = marks(started, min(contract.seconds_left(task), LOCAL_SECONDS))
    briefing = await read_brief(task)
    trace.event("briefing", "done", **briefing.model_dump(exclude={"submission_steps"}))
    sites = {site["siteId"]: site for site in task["access"]}
    contexts = dict(zip(sites, await browsers.more_contexts(page, len(sites))))
    opened: Counter = Counter()
    research_tabs: list[Tab] = []

    async def open_tab(site_id: str, label: str) -> Tab:
        tab = Tab(sites[site_id], contexts[site_id], trace, label=label)
        try:
            await tab.start()
        except BaseException:  # given up while loading (a time limit): do not leave the tab loading behind
            await close_tab(tab)
            raise
        return tab

    async def open_session(site_id: str) -> Tab:
        """A new signed-in tab on that website for the research; its tabs share the website's browser context."""
        opened[site_id] += 1
        tab = Tab(sites[site_id], contexts[site_id], trace, label=f"{site_id}/{opened[site_id]}")
        research_tabs.append(tab)  # listed before it loads, so a tab cut off while opening is closed too
        await tab.start()
        return tab

    # The bid form and the mailbox lookups for signing work in tabs of their own, so nothing the research does on
    # the same websites navigates under them.
    bid_tab = asyncio.create_task(open_tab(briefing.bid_site, f"{briefing.bid_site}/bid"))
    mailbox = Mailbox(lambda: open_tab(briefing.mail_site, f"{briefing.mail_site}/sign"), briefing, sign_by)
    address = asyncio.create_task(mailbox.address())

    findings = await gather_findings(briefing, open_session, research_by, entry_by, trace)
    for tab in research_tabs:  # research is over: its tabs must not keep loading beside the bid form
        await close_tab(tab)
    mailbox_address = (findings.mail.mailbox_address if findings else "") or await address
    if not address.done():
        address.cancel()
    plan = make_plan(findings, briefing, mailbox_address)
    trace.event("plan", "done", seconds_left=round(sign_by - time.monotonic()), decision=plan.decision,
                discount_percent=str(plan.discount_percent), total_net=str(plan.total_net),
                enter=[entry.position for entry in plan.entries if enterable(entry)],
                omit={entry.position: entry.reason for entry in plan.entries if not enterable(entry)},
                notes=plan.notes)

    result = None
    try:
        result = await submit_bid(await bid_tab, mailbox, briefing, plan, sign_by, mailbox_address)
    except Exception as error:  # a crashed tab or browser: sign the bare bid in a new tab rather than nothing
        trace.event("bid", "failed", reason=f"{type(error).__name__}: {str(error)[:200]}")
        try:
            plan = fallback_plan(briefing.tender_reference, findings, "bid entry failed")
            retry_tab = await open_tab(briefing.bid_site, f"{briefing.bid_site}/bid-2")
            result = await submit_bid(retry_tab, mailbox, briefing, plan, sign_by, mailbox_address)
        except Exception as again:  # nothing more to try; end the run without a traceback
            trace.event("bid", "failed", reason=f"{type(again).__name__}: {str(again)[:200]}")
    trace.event("result", "done", seconds=round(time.monotonic() - started),
                result=result.model_dump() if result else None)
    return None


async def main() -> int:
    task, token = contract.load_run()
    sites = ", ".join(f"{site.get('siteId')} {site.get('url')}" for site in task["access"])
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    async with async_playwright() as playwright:
        browser, page = await browsers.connect(playwright)  # the other browsers: browsers.more_contexts in solve()
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
