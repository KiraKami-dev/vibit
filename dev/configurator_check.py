"""
Dev check of the bid worker's position filling with a hand-made answer: never part of the package.

The answer file is JSON {"answer": <BidResult.answer() shape>, "attributes": {position: {name: value}}}. Keep such
files under dev/local/ (gitignored): they hold values read from the sites.

Live, on the scouting run: the agent's flow up to the signing page, in fresh browser contexts (the wizard keeps
its state per browser session, so other users do not interfere), stopped before the TAN request
(AGENT_STOP_BEFORE_SIGN=1) since TANs are shared; the trace shows the summary check:
    eval "$(atira scout --env)"; (cd agent && HEADLESS=1 uv run --no-sync python ../dev/configurator_check.py \\
        ../dev/local/answer.json)
Offline, against a local mock wizard (no logins, a fake mailbox that reads the mock's TAN from MOCK/_tan):
    (cd agent && uv run --no-sync python ../dev/configurator_check.py --mock http://127.0.0.1:8899/bid ANSWER.json)
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlopen

AGENT = Path(__file__).resolve().parent.parent / "agent"
sys.path.insert(0, str(AGENT))

import agent  # noqa: E402
import browsers  # noqa: E402
import run as contract  # noqa: E402
from briefing import Briefing  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from submit import Mailbox, SigningTan, submit_bid  # noqa: E402
from tracing import Trace  # noqa: E402


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

    async def tan(self, answer: str = "") -> SigningTan | None:
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


async def live(path: str) -> int:
    """The agent's own flow with this answer, stopped before the TAN request (AGENT_STOP_BEFORE_SIGN=1)."""
    answer, attributes = load(path)
    task, _ = contract.load_run()
    if task.get("run_id"):
        raise SystemExit("configurator_check runs only locally on the scouting run")
    os.environ["AGENT_STOP_BEFORE_SIGN"] = "1"  # TANs are shared by everyone on the scouting logins
    async with async_playwright() as playwright:
        _, page = await browsers.connect(playwright)
        await agent.solve(task, page, answer, attributes)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--mock"]:
        asyncio.run(mock(sys.argv[2], sys.argv[3]))
    else:
        sys.exit(asyncio.run(live(sys.argv[1])))
