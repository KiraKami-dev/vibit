"""
Drives the run's websites one action at a time while `sim/capture.py` records what they answer.

`serve` keeps one signed-in browser context per website open and listens on a local port; every other call sends
one action to it and prints the page as the agent would see it (text with `[eN ...]` refs). Each action ends with
a snapshot, so a path walked by hand (a gate, a search, the bid form) is recorded step by step.

    HEADLESS=1 uv run --project agent python sim/drive.py serve &        # once; `stop` ends it
    uv run --project agent python sim/drive.py <tab> look [offset]
    ... <tab> goto <url or path> | click <ref> | fill <ref> <text> [submit] | select <ref> <option>
    ... <tab> check <ref> [off] | back | crawl [pages] | halt | label <text> | open <siteId>

A tab is named after its website; `open` adds a further tab on a website's sign-in under the name given.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent))

import capture  # noqa: E402
from browser_tools import SiteSession, _click  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from tracing import Trace  # noqa: E402

PORT = int(os.environ.get("DRIVE_PORT", "8791"))
SHOWN = 6000  # characters of a view printed per call; `look <offset>` shows the rest


class Driver:
    def __init__(self, task: dict, trace: Trace):
        self.task, self.trace = task, trace
        self.tabs: dict[str, SiteSession] = {}
        self.recorders: dict[str, capture.Recorder] = {}  # by siteId
        self.done = asyncio.Event()

    async def start(self, playwright, wanted: list[str]) -> None:
        async def one(site: dict) -> None:
            session, recorder = await capture.open_site(playwright, site, self.trace)
            self.tabs[site["siteId"]], self.recorders[site["siteId"]] = session, recorder
            await recorder.snapshot(session, site["url"], "start page")
        await asyncio.gather(*(one(s) for s in self.task["access"] if not wanted or s["siteId"] in wanted))

    async def run(self, tab: str, verb: str, args: list[str]) -> str:
        if verb == "stop":
            self.done.set()
            return "stopping"
        if verb == "open":
            site = self.tabs[args[0]].site
            session = SiteSession(site, self.tabs[args[0]].context, self.trace, label=tab)
            await session.start()
            self.tabs[tab] = session
            return await self._show(tab, site["url"], f"open {tab}")
        session = self.tabs[tab]
        recorder = self.recorders[session.site["siteId"]]
        if verb == "label":
            recorder.label = " ".join(args)
            return f"label: {recorder.label}"
        if verb == "halt":
            recorder.halt = True
            return "the crawl ends after its current page"
        if verb == "look":
            return (await session.look())[int(args[0]) if args else 0:][:SHOWN]
        if verb == "crawl":
            async with asyncio.timeout(3600):
                pages = await capture.crawl(session, recorder, int(args[0]) if args else capture.MAX_PAGES)
            return f"{pages} pages, {recorder.count} responses so far"
        if verb == "goto":
            target = urljoin(session.page.url, args[0])
            async with session.lock:
                await session._goto(target)
            return await self._show(tab, target, f"goto {args[0]}")
        if verb == "back":
            async with session.lock:
                try:
                    await session.page.go_back(wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                await session.settle()
            return await self._show(tab, session.page.url, "back")
        ref = args[0]
        if verb == "click":
            fn = _click
        elif verb == "fill":
            async def fn(element):
                await element.fill(args[1], timeout=5000)
                if args[2:] == ["submit"]:
                    await element.press("Enter")
        elif verb == "select":
            async def fn(element):
                try:
                    await element.select_option(label=args[1], timeout=5000)
                except Exception:
                    await element.select_option(args[1], timeout=5000)
        elif verb == "check":
            async def fn(element):
                await element.set_checked(args[1:] != ["off"], timeout=5000)
        else:
            return f"unknown action {verb!r}"
        before = await session.act(verb, ref, fn, typed=" ".join(args[1:]))
        note = f"{verb} {' '.join(args)}"
        await recorder.snapshot(session, session.page.url, note)
        return before[:SHOWN]

    async def _show(self, tab: str, requested: str, note: str) -> str:
        session = self.tabs[tab]
        view = await session.look()
        await self.recorders[session.site["siteId"]].snapshot(session, requested, note)
        return view[:SHOWN]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            tab, verb, *args = json.loads(await reader.readline())
            answer = await self.run(tab, verb, args)
        except Exception as error:
            answer = f"failed: {type(error).__name__}: {error}"
        writer.write(answer.encode("utf-8"))
        await writer.drain()
        writer.close()


async def serve(wanted: list[str]) -> None:
    driver = Driver(capture.scouting_run(), Trace(str(capture.CAPTURED / "_traces")))
    async with async_playwright() as playwright:
        await driver.start(playwright, wanted)
        server = await asyncio.start_server(driver.handle, "127.0.0.1", PORT, limit=1 << 20)
        print(f"driving {sorted(driver.tabs)} on port {PORT}", flush=True)
        async with server:
            await driver.done.wait()
        for recorder in driver.recorders.values():
            await recorder.drain()


async def send(words: list[str]) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT, limit=1 << 22)
    writer.write((json.dumps(words) + "\n").encode("utf-8"))
    await writer.drain()
    print((await reader.read()).decode("utf-8"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        asyncio.run(serve(sys.argv[2:]))
    else:
        asyncio.run(send(sys.argv[1:] if len(sys.argv) > 2 else ["-", *sys.argv[1:]]))
