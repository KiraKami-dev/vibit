"""
Check the mailbox survey alone: read the brief, plan the record kinds, then run the survey in N tabs side by side
(each one a full survey of its own) and print what each listed, sorted and read. Read-only: no TAN is requested.

Run from the repo root with the scouting logins and the models proxy:
    eval "$(atira scout --env)"; HEADLESS=1 uv run --project agent python dev/survey_check.py [N]
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import browsers  # noqa: E402
import research_mail  # noqa: E402
import run as contract  # noqa: E402
from briefing import read_brief  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from tracing import Trace  # noqa: E402


async def main() -> None:
    copies = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    task, _ = contract.load_run()
    trace = Trace()
    briefing = await read_brief(task)
    sites = {s["siteId"]: s for s in task["access"]}
    kinds = await research_mail.plan_searches(task["brief"], {k: v["url"] for k, v in sites.items()})
    kinds = [kind for kind in kinds if kind.site == briefing.mail_site] or kinds
    async with async_playwright() as playwright:
        (context,) = await browsers.contexts(playwright, 1)
        first = SiteSession(sites[briefing.mail_site], context, trace)
        await first.start()
        others = [SiteSession(sites[briefing.mail_site], context, trace) for _ in range(copies - 1)]
        await asyncio.gather(*(s.start() for s in others))
        sessions = [first, *others]
        for i, session in enumerate(sessions, 1):  # a name of its own per tab, so the trace tells them apart
            session.site = {**session.site, "siteId": f"{briefing.mail_site}:survey{i}"}

        async def one(session: SiteSession):
            started = time.monotonic()
            found = await research_mail.survey(session, kinds, briefing.tender_reference,
                                               deadline=time.monotonic() + 400)
            return round(time.monotonic() - started, 1), found

        results = await asyncio.gather(*(one(s) for s in sessions), return_exceptions=True)
    print("\n===== surveys")
    for session, result in zip(sessions, results):
        if isinstance(result, BaseException):
            print(f"{session.name}: failed: {type(result).__name__}: {result}")
            continue
        seconds, found = result
        missing = sorted({kind.name for kind in kinds} - set(found))
        print(f"{session.name}: {seconds}s, {len(found)} of {len(kinds)} kinds read; none for: {missing}")
        for name, result in sorted(found.items()):
            print(f"    {name}: {len(result.kept)} message(s), {len(result.records)} record(s)")


if __name__ == "__main__":
    asyncio.run(main())
