"""Nothing in a worker's step may hang the run: large views stay fast, a frozen page is cut off, a blocked loop is
reported (invented pages in a local Chromium: uv run playwright install chromium)."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_tools  # noqa: E402
import extract  # noqa: E402
import loopwatch  # noqa: E402
import research  # noqa: E402
from browser_tools import SiteSession, unseen_values  # noqa: E402
from extract import changes, locate  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from tracing import Trace  # noqa: E402

SITE = {"siteId": "demo", "url": "https://example.com/"}


def big_view(rows: int, value: int = 0) -> str:
    return "\n".join(f'| Row {i} | [e{i} checkbox "pick {i}" unchecked] | {i * 7 + value} EUR |' for i in range(rows))


class LargeTextTest(unittest.TestCase):
    def test_diff_of_large_views_is_fast(self):
        old, new = big_view(6000), big_view(6000, value=1)  # about 250k characters each
        self.assertGreater(len(old), 200_000)
        started = time.monotonic()
        result = changes(old, new)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsNone(result)  # too long to diff: the view goes whole

    def test_grounding_is_fast_on_long_tokens_and_a_large_history(self):
        seen = "\n".join(big_view(2000, value=v) for v in range(25))  # about 2 MB of earlier views
        text = "a" * 200_000 + " order Q9Z and R-99-x"  # one long token without a digit: quadratic before
        started = time.monotonic()
        missing = unseen_values(text, seen)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(missing, ["Q9Z", "R-99-x"])
        self.assertEqual(unseen_values("Row 12 costs 91  EUR", "| Row 12 |\n  91   EUR"), [])


class FrozenPageTest(unittest.TestCase):
    def test_a_frozen_page_is_cut_off_and_others_go_on(self):
        async def run():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context()
                frozen, other = await context.new_page(), await context.new_page()
                await frozen.set_content("<p>Stock: 12</p><button>Go</button>")
                await other.set_content("<p>Stock: 13</p>")
                await frozen.evaluate("setTimeout(() => { while (true) {} }, 50)")
                await asyncio.sleep(0.5)
                hung = False
                try:  # what observe did before: no bound at all
                    await asyncio.wait_for(frozen.evaluate("1"), 2)
                except TimeoutError:
                    hung = True
                with tempfile.TemporaryDirectory() as directory, \
                        mock.patch.object(extract, "WALK_SECONDS", 1.0), \
                        mock.patch.object(extract, "FRAME_SECONDS", 1.0), \
                        mock.patch.object(browser_tools, "FRAME_SECONDS", 1.0):
                    session = SiteSession(SITE, context, Trace(directory), pages=[frozen])
                    started = time.monotonic()
                    view = await session.look(fresh=True)
                    ref = await locate(frozen, "e1")
                    seconds = time.monotonic() - started
                    alive = await SiteSession(SITE, context, Trace(directory), pages=[other]).look(fresh=True)
                    session.trace.file.close()
                await browser.close()
                return hung, view, ref, seconds, alive
        hung, view, ref, seconds, alive = asyncio.run(run())
        self.assertTrue(hung)
        self.assertIn("does not respond", view)
        self.assertIsNone(ref)
        self.assertLess(seconds, 10)
        self.assertIn("Stock: 13", alive)

    def test_look_on_a_large_page_is_fast(self):
        rows = "".join(f"<tr><td>Row {i}</td><td><input type=checkbox aria-label='pick {i}'></td>"
                       f"<td>{i * 7} EUR</td></tr>" for i in range(5000))

        async def run():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context()
                page = await context.new_page()
                await page.set_content(f"<table>{rows}</table>")
                with tempfile.TemporaryDirectory() as directory:
                    session = SiteSession(SITE, context, Trace(directory), pages=[page])
                    await session.look(fresh=True)
                    await page.evaluate("document.querySelector('td').textContent = 'Row x'")
                    with mock.patch.object(browser_tools, "observe", wraps=browser_tools.observe) as walk:
                        started = time.monotonic()
                        view = await session.look(brief=True)
                        total = time.monotonic() - started
                        walk_started = time.monotonic()
                        await walk(page, "x")
                        walk_seconds = time.monotonic() - walk_started
                    session.trace.file.close()
                await browser.close()
                return view, len(session.view), total, walk_seconds
        view, size, total, walk_seconds = asyncio.run(run())
        self.assertGreater(size, 100_000)
        self.assertIn("Row x", view)
        self.assertLess(total - walk_seconds, 1.0)  # the Python side (diff, history, trace) beyond the page walk


class Events:
    """A Trace stand-in that keeps the events."""

    def __init__(self):
        self.events, self.last = [], 0.0

    def event(self, name, kind, quiet=False, **fields):
        self.events.append((name, kind, fields))


class FakePage:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed

    def on(self, event, handler):
        pass

    async def close(self):
        self.closed = True


class DeafContext:
    """A browser context whose calls never return."""

    browser = None

    async def add_init_script(self, script):
        await asyncio.Event().wait()

    async def new_page(self):
        await asyncio.Event().wait()


class HungTabTest(unittest.TestCase):
    """A tab that hangs costs its own attempt: no browser call and no wait on it is without a bound."""

    def test_a_start_ends_when_the_browser_opens_no_tab(self):
        async def run():
            session = SiteSession(SITE, DeafContext(), Events())
            started = time.monotonic()
            with mock.patch.object(browser_tools, "TAB_SECONDS", 0.3), \
                    mock.patch.object(browser_tools, "RACE_AFTER", 0.1):
                with self.assertRaises(TimeoutError):
                    await session.start(attempts=1, until=time.monotonic() + 2)
            return time.monotonic() - started, session
        seconds, session = asyncio.run(run())
        self.assertLess(seconds, 5)
        self.assertIn("start_failed", [kind for _, kind, _ in session.trace.events])
        self.assertTrue(session.settled.is_set())

    def test_a_start_ends_when_its_attempt_never_returns(self):
        async def never(self, end):
            await asyncio.Event().wait()

        async def run():
            session = SiteSession(SITE, DeafContext(), Events())
            started = time.monotonic()
            with mock.patch.object(SiteSession, "_attempt", never), \
                    mock.patch.object(browser_tools, "RACE_AFTER", 0.1), \
                    mock.patch.object(browser_tools, "RACE_GRACE", 0.2):
                with self.assertRaises(TimeoutError):
                    await session._race(time.monotonic() + 0.3, started)
            return time.monotonic() - started
        self.assertLess(asyncio.run(run()), 5)

    def test_a_reading_tab_that_stopped_answering_is_replaced(self):
        class Context:
            def __init__(self):
                self.opened = []

            async def new_page(self):
                self.opened.append(FakePage())
                return self.opened[-1]

        async def run(disposable: bool):
            dead, context, visits = FakePage(), Context(), []

            async def navigate(self, url):  # the old tab answers no script call, a new one does
                visits.append((self.page, url))
                self.hung = self.page is dead

            session = SiteSession(SITE, context, Events(), pages=[dead])
            session.disposable = disposable
            with mock.patch.object(SiteSession, "_navigate", navigate):
                await session._goto("https://example.com/record")
            return dead, context, visits, session
        dead, context, visits, session = asyncio.run(run(True))
        self.assertEqual([page for page, _ in visits], [dead, context.opened[0]])
        self.assertEqual({url for _, url in visits}, {"https://example.com/record"})
        self.assertTrue(dead.closed)
        self.assertEqual(session.pages, context.opened)
        self.assertFalse(session.hung)
        dead, context, visits, session = asyncio.run(run(False))  # e.g. a form: its tab is never swapped
        self.assertEqual([page for page, _ in visits], [dead])
        self.assertEqual((context.opened, dead.closed, session.hung), ([], False, True))

    def test_erp_checks_and_tabs_are_bounded(self):
        class Locator:
            async def count(self):
                await asyncio.Event().wait()

        async def run():
            trace = Events()
            erp = research.Erp({"siteId": "erp", "url": "https://example.com/"}, None, trace,
                               deadline=time.monotonic() + 1000)
            session = SimpleNamespace(page=SimpleNamespace(locator=lambda selector: Locator()))
            started = time.monotonic()
            with mock.patch.object(research, "FRAME_SECONDS", 0.2):
                form = await erp._signin_form(session)

            async def never(tag, url=None):
                await asyncio.Event().wait()

            erp._tab = never
            tab = await erp._worker_tab("M-1", seconds=0.2)
            return form, tab, time.monotonic() - started, trace, erp
        form, tab, seconds, trace, erp = asyncio.run(run())
        self.assertFalse(form)
        self.assertIsNone(tab)
        self.assertLess(seconds, 3)
        self.assertIn("tab_failed", [kind for _, kind, _ in trace.events])

    def test_an_erp_worker_ends_on_silence_not_on_slowness(self):
        async def run(page_every: float | None, work_seconds: float, view: str = "Stock: 12"):
            trace = Events()
            erp = research.Erp({"siteId": "erp", "url": "https://example.com/"}, None, trace,
                               deadline=time.monotonic() + 1000)
            session = SimpleNamespace(name="erp:M-1", stamp=0, hung=False, pages=[], view=view)

            async def worker(session, *args, **kwargs):  # a new view every `page_every` seconds, or none at all
                end = time.monotonic() + work_seconds
                while time.monotonic() < end:
                    await asyncio.sleep(page_every or 0.05)
                    session.stamp += 1 if page_every else 0
                return "done", {}

            started = time.monotonic()
            with mock.patch.object(research, "run_worker", worker), \
                    mock.patch.object(research, "STALL_SECONDS", 0.6):
                result = await erp._work(session, "goal", str)
            return result, time.monotonic() - started, session.hung, [kind for _, kind, _ in trace.events]
        # slow but alive: it takes several times the silence limit and is left to finish
        result, seconds, hung, kinds = asyncio.run(run(0.2, 2.0))
        self.assertEqual((result, hung, kinds), (("done", {}), False, []))
        self.assertGreater(seconds, 1.9)
        # no new view at all: given up after the limit, and its tab is not reused
        result, seconds, hung, kinds = asyncio.run(run(None, 30.0))
        self.assertEqual((result, hung, kinds), ((None, {}), True, ["stalled"]))
        self.assertLess(seconds, 5)
        # looks at a page that does not answer are no progress either
        result, seconds, hung, kinds = asyncio.run(run(0.2, 30.0, view="(the page does not respond: TimeoutError)"))
        self.assertEqual((result, hung, kinds), ((None, {}), True, ["stalled"]))
        self.assertLess(seconds, 5)


class WatchdogTest(unittest.TestCase):
    def test_a_blocked_loop_dumps_the_thread_stacks(self):
        with tempfile.TemporaryFile("w+") as out:
            async def run():
                dog = loopwatch.Watchdog(stale=0.5, repeat=60, out=out).start()
                await asyncio.sleep(1.2)
                time.sleep(2.5)  # synchronous work on the loop
                await asyncio.sleep(0.1)
                dog.stop()
                return dog.dumps
            dumps = asyncio.run(run())
            out.seek(0)
            log = out.read()
        self.assertEqual(dumps, 1)
        self.assertIn("the event loop has not run", log)
        self.assertIn("test_hang.py", log)  # the blocking line's file

    def test_a_silent_run_dumps_the_task_stacks(self):
        with tempfile.TemporaryFile("w+") as out:
            async def stuck():
                await asyncio.Event().wait()

            async def run():
                started = time.monotonic()
                dog = loopwatch.Watchdog(activity=lambda: started, silent=0.5, out=out).start()
                task = asyncio.create_task(stuck(), name="stuck-worker")
                await asyncio.sleep(2.0)
                task.cancel()
                dog.stop()
            asyncio.run(run())
            out.seek(0)
            log = out.read()
        self.assertIn("no trace event", log)
        self.assertIn("stuck", log)


if __name__ == "__main__":
    unittest.main()
