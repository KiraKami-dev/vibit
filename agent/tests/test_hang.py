"""Nothing in a worker's step may hang the run: large views stay fast, a frozen page is cut off, a blocked loop is
reported (invented pages in a local Chromium: uv run playwright install chromium)."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import browser_tools  # noqa: E402
import extract  # noqa: E402
import loopwatch  # noqa: E402
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
