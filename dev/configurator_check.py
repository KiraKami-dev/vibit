"""
Dev check of the bid worker's position filling with a hand-made answer: never part of the package.

The answer file is JSON {"answer": <BidResult.answer() shape>, "attributes": {position: {name: value}}}. Keep such
files under dev/local/ (gitignored): they hold values read from the sites.

Live, on the scouting run: everything up to the signing page in a fresh browser context (the wizard keeps its
state per browser session, so other users do not interfere); it never requests a TAN, since TANs are shared:
    eval "$(atira scout --env)"; (cd agent && HEADLESS=1 uv run --no-sync python ../dev/configurator_check.py \\
        ../dev/local/answer.json)
Offline, against a local mock wizard (no logins, a fake mailbox that reads the mock's TAN from MOCK/_tan):
    (cd agent && uv run --no-sync python ../dev/configurator_check.py --mock http://127.0.0.1:8899/bid ANSWER.json)
"""
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.request import urlopen

AGENT = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(AGENT))

import browsers  # noqa: E402
import run as contract  # noqa: E402
from briefing import Briefing, read_brief  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from configurator import complete_positions  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from submit import Mailbox, PositionDesk, SigningTan, SubmissionResult, bid_goal, submit_bid  # noqa: E402
from tracing import Trace  # noqa: E402
from worker import run_worker  # noqa: E402


def load(path: str) -> tuple[dict, dict]:
    file = Path(path) if Path(path).exists() else AGENT.parent / path  # else relative to the repo root
    data = json.loads(file.read_text(encoding="utf-8"))
    return data["answer"], data.get("attributes", {})


class MockMailbox(Mailbox):
    """Stands in for the webmail: the mock wizard exposes its current TAN."""

    def __init__(self, base: str):
        self.base, self.last_tan = base, ""

    async def address(self) -> str:
        return "bids@example.com"

    async def tan(self) -> SigningTan | None:
        with urlopen(self.base + "/_tan") as response:
            tan = json.load(response)["tan"]
        return SigningTan(tan=tan, subject="Signing TAN", received="now") if tan else None


async def mock(url: str, path: str) -> None:
    answer, attributes = load(path)
    base = url.split("/bid")[0]
    briefing = Briefing(tender_reference=answer["tender_reference"], company_name="Example Hydraulics GmbH",
                        vat_id="DE000000000", scenario_date="2026-01-01", bid_site="bid", mail_site="mail",
                        submission_steps="Enter bidder details, the offer type, every item, the terms and the "
                                         "declarations, then sign with a TAN from your mailbox.")
    trace = Trace()
    async with async_playwright() as playwright:
        _, page = await browsers.connect(playwright)
        session = SiteSession({"siteId": "bid", "url": url, "credentials": {}}, page.context, trace)
        await session.start()
        started = time.monotonic()
        session.seen += "\nbids@example.com"
        result = await submit_bid(session, MockMailbox(base), briefing, time.monotonic() + 900, answer, attributes)
        print("RESULT", result, f"{time.monotonic() - started:.0f}s")
    with urlopen(base + "/_state") as response:
        print("STATE", json.dumps(json.load(response), indent=1))


SIGNING = """- request the signing TAN, call get_signing_tan() right away, enter that TAN, check the summary and submit
  bindingly. Browser confirmation dialogs are accepted for you.
Return once the website confirms the submission (or shows why it failed)."""
NO_SIGNING = """- go on up to the final review and signing page, then STOP: never request a TAN (TANs are shared with
  other bidders) and never submit. Return there with submitted=false and the review page's summary of positions
  and totals word for word as confirmation."""


async def live(path: str) -> int:
    """Everything up to the signing page in a fresh browser context; no TAN request, nothing submitted."""
    answer, attributes = load(path)
    task, _ = contract.load_run()
    if task.get("run_id"):
        raise SystemExit("configurator_check runs only locally on the scouting run")
    trace = Trace()
    briefing = await read_brief(task)
    sites = {site["siteId"]: site for site in task["access"]}
    async with async_playwright() as playwright:
        _, page = await browsers.connect(playwright)
        bid_context, mail_context = await browsers.more_contexts(page, 2)
        bid = SiteSession(sites[briefing.bid_site], bid_context, trace)
        mail = SiteSession(sites[briefing.mail_site], mail_context, trace)
        await asyncio.gather(bid.start(), mail.start())
        deadline = time.monotonic() + 1200
        mailbox = Mailbox(mail, briefing, deadline)
        entries = complete_positions(answer, attributes)

        async def bidder_contact_email(reason: str = "") -> str:
            """The bidder's contact email address, read from the bidder's own webmail."""
            found = await mailbox.address()
            bid.seen += "\n" + found
            return found or "not found"

        goal = bid_goal(briefing, answer, entries)
        assert SIGNING in goal, "the signing step changed; update NO_SIGNING"
        goal = goal.replace(SIGNING, NO_SIGNING)
        started = time.monotonic()
        tools = (bidder_contact_email, *PositionDesk(entries, deadline).tools)
        result, _ = await run_worker(bid, goal, SubmissionResult, deadline=deadline,
                                     max_steps=70 + 3 * len(entries), thinking=True, tools=tools)
        print("RESULT", result, f"{time.monotonic() - started:.0f}s")
        print("FINAL VIEW\n" + await bid.look())
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--mock"]:
        asyncio.run(mock(sys.argv[2], sys.argv[3]))
    else:
        sys.exit(asyncio.run(live(sys.argv[1])))
