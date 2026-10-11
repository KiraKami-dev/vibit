"""
The mirror through a real browser: the agent's own helpers sign in, and the agent's view of each mirrored page is
compared with its view of the website's page as recorded.

Needs a recording in sim/captured/ and the agent's environment. From the repo root:
    HEADLESS=1 uv run --project agent python sim/tests/browser_check.py [pages per website]
"""
import asyncio
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "agent"))

import browsers  # noqa: E402
import mirror  # noqa: E402
from browser_tools import SiteSession, _plain  # noqa: E402
from extract import observe  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from tracing import Trace  # noqa: E402

PER_SITE = int(sys.argv[1]) if sys.argv[1:] else 25
RANDOM = re.compile(r"\[img: [^\]]*: [A-Z]*-?\d{5,}\]")  # the number an entry check draws anew each time


def same(mirrored: str, website: str, source: str) -> bool:
    """The same lines in any order (some lists are shuffled on every page). A line only the mirror's view has is
    fine if the recorded page contains its text: a notice a script reveals a moment after loading."""
    def tidy(text: str) -> list[str]:
        def line_of(line: str) -> str:  # look-alike controls on one line come in a new order on every load
            controls = re.findall(r"\[[^\]]*\]", line)
            return (re.sub(r"\[[^\]]*\]", "", line).strip() + " " + " ".join(sorted(controls))).strip()
        def date(m):  # the same date is written day first on one load and month first on the next
            day, month = (m.group(1), m.group(2)) if m.group(3).startswith("TT") else (m.group(2), m.group(1))
            return f"{day}.{month}."
        text = re.sub(r"(\d\d)/(\d\d) \((TT/MM|MM/TT)\)", date, text)
        return sorted(line_of(RANDOM.sub("[img]", line)) for line in _plain(text).splitlines())
    ours, theirs = tidy(mirrored), tidy(website)
    late = [line for line in ours if line not in theirs]
    return all(line in ours for line in theirs) and all(line in source for line in late)


async def check(playwright, site: mirror.Site, trace: Trace) -> tuple[int, int, list[str]]:
    (context,) = await browsers.contexts(playwright, 1)
    access = {"siteId": site.id, "url": site.origin, "credentials": site.credentials}
    session = SiteSession(access, context, trace)
    await session.start()
    for _ in range(2):  # the entry checks ask for a number the model copies; here it is read off the page
        page = session.page
        shown = mirror.NUMBER.search(await page.content())
        box = page.locator('input[name="receipt"]')
        if not shown or not await box.count():
            break
        for radio in await page.locator("input[type=radio]").all():
            form_path = next(iter(site.rec.passes))
            if await radio.get_attribute("value") in site.rec.passes[form_path].values():
                await radio.check()
        await box.fill(shown.group(1))
        for tick in await page.locator('form:has(input[name="receipt"]) input[type=checkbox]').all():
            await tick.check()
        await page.locator('form:has(input[name="receipt"]) button[type=submit]').last.click()
        await session.settle()
    for checked in site.rec.checks:
        await session._goto(site.origin + checked)
        page = session.page
        shown = mirror.NUMBER.search(await page.content())
        if shown and await page.locator('input[name="receipt"]').count():
            wanted = site.rec.passes[site.rec.checks[checked]]
            for radio in await page.locator("input[type=radio]").all():
                if await radio.get_attribute("value") in wanted.values():
                    await radio.check()
            await page.locator('input[name="receipt"]').fill(shown.group(1))
            for tick in await page.locator('form:has(input[name="receipt"]) input[type=checkbox]').all():
                await tick.check()
            await page.locator('form:has(input[name="receipt"]) button[type=submit]').last.click()
            await session.settle()
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((mirror.CAPTURED / site.id / "pages").glob("*.json"))]
    latest = {}
    for record in records:  # the latest recorded view of each address that was opened as a page
        url = record["url"].replace("{{origin}}", "")
        if record["requested"].replace("{{origin}}", "").rstrip("/") == url.rstrip("/") and "(not readable" not in record["view"]:
            latest[url or "/"] = record
    skip = ("/login", "/consent", site.rec.entry or "-", site.bidform.path if site.bidform else "-")
    compared, differing, examples = 0, 0, []
    for url, record in list(latest.items()):
        parts = urlsplit(url)
        if compared >= PER_SITE or url.startswith(skip):
            continue
        if site.id == site.world.mail_site and not dict(mirror.canon(parts.query)).get("q"):
            continue  # the TAN list: it grows on the website with every request, the recorded view is of another day
        if (parts.path or "/", mirror.canon(parts.query)) not in site.rec.pages:
            continue  # only ever the answer to a form, not an address to open
        await session._goto(site.origin + url)
        await session.finish_loading()
        view = await observe(session.page, "check")
        compared += 1
        source = "".join((mirror.CAPTURED / site.id / "pages" / f["dom"]).read_text(encoding="utf-8") for f in record["frames"])
        if not same(view, record["view"], html.unescape(source)):
            differing += 1
            got, want = set(_plain(view).splitlines()), set(_plain(record["view"]).splitlines())
            examples.append(f"{url}\n   only mirror: {sorted(got - want)[:3]}\n   only website: {sorted(want - got)[:3]}")
    await context.close()
    return compared, differing, examples


async def main() -> None:
    mirror.BASE_PORT = 8861
    world = mirror.World("browser")
    world.serve()
    trace = Trace(str(mirror.CAPTURED / "_traces"))
    failed = False
    async with async_playwright() as playwright:
        for site in world.sites.values():
            compared, differing, examples = await check(playwright, site, trace)
            print(f"{site.id}: {compared - differing} of {compared} pages read the same as on the website", flush=True)
            for example in examples[:4]:
                print("  differs:", example[:700], flush=True)
            failed = failed or differing > 0 or compared == 0
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
