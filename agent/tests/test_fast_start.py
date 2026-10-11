"""
Starts that hang in their browser context (local Chromium, a local site): a navigation that never commits, a sign-in
post that never returns, a page that stops answering script calls, and a page that renders but never finishes
loading. The site itself answers at once in a fresh context, as in the sandbox runs where a fresh context started
in under a second after minutes on the old tab. Each must be recovered in seconds, not minutes; the slow-but-alive
case (the first tab answers first) must still keep its own tab. Limits are scaled down as in test_slow_start.
"""
import asyncio
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402
from test_slow_start import LOGIN, SCALED, STALL, Recorder, serve, site  # noqa: E402

import browser_tools  # noqa: E402
from browser_tools import SiteSession  # noqa: E402

RACE = dict(RACE_AFTER=2, ATTEMPT_SECONDS=(12, 15), START_SECONDS=15)
HUNG = 600  # a request held this long never answers within a test


def scaled(**over):
    for key, value in {**SCALED, **RACE, **over}.items():
        mock.patch.object(browser_tools, key, value).start()


def serve_login():
    """A site whose start page is a sign-in form; the post answers at once with the dashboard."""
    counts = {"GET /": 0, "POST /login": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: str) -> None:
            data = f"<!doctype html><html><body>{body}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            counts["GET /"] += 1
            self._send(f"<h1>Portal sign-in</h1>{LOGIN}")

        def do_POST(self):
            counts["POST /login"] += 1
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._send("<h1>Welcome dashboard</h1><a href='/x'>Open tenders</a>")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, counts


def serve_streaming():
    """A home page that renders at once but whose response never ends (DOMContentLoaded never fires)."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            self.wfile.write(b"<!doctype html><html><body><h1>Portal home</h1><a href='/x'>Open tenders</a>"
                             + b" " * 4096)
            self.wfile.flush()
            time.sleep(HUNG)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def hang(route):
    """A request of a broken context: never answered (the context's connection is stuck)."""
    await asyncio.sleep(HUNG)


async def start(server, broken=None, **kwargs):
    """Start a session on `server` in a context `broken` prepares; returns (session, view, seconds, own_context)."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        if broken:
            await broken(context)
        session = SiteSession(site(server), context, Recorder())
        began = time.monotonic()
        try:
            view = await session.start(until=time.monotonic() + 60, **kwargs)
        finally:
            seconds = time.monotonic() - began
        own = session.context is context
        await browser.close()
        return session, view, seconds, own


def ok_event(session) -> dict:
    return next(fields for _, kind, fields in session.trace.events if kind == "start_ok")


class FastStartTest(unittest.TestCase):
    def tearDown(self):
        mock.patch.stopall()

    def test_a_navigation_that_never_commits_is_raced_by_a_fresh_context(self):
        """Sandbox: the tab stayed on about:blank for 210 s; a fresh context opened the site in under a second."""
        server, counts = serve(slow_home=False, slow_login=False)
        scaled()

        async def broken(context):
            await context.route("**/*", hang)

        session, view, seconds, own = asyncio.run(start(server, broken))
        server.shutdown()
        self.assertIn("Portal home", view)
        self.assertLess(seconds, browser_tools.RACE_AFTER + 4)
        self.assertFalse(own)  # the session now works in the fresh context
        self.assertTrue(ok_event(session)["raced"])
        self.assertEqual(ok_event(session)["attempt"], 1)
        phases = {f.get("phase") for _, kind, f in session.trace.events if kind == "start_wait"}
        self.assertTrue(phases & {"goto", "commit"})  # the first tab was still waiting for its page

    def test_a_sign_in_post_that_never_returns_is_raced(self):
        """Sandbox: the sign-in page showed, the post never came back, and the tab waited 240 s."""
        server, counts = serve_login()
        scaled()

        async def broken(context):
            async def post_hangs(route):
                if route.request.method == "POST":
                    await hang(route)
                else:
                    await route.continue_()
            await context.route("**/*", post_hangs)

        session, view, seconds, own = asyncio.run(start(server, broken))
        server.shutdown()
        self.assertIn("Welcome dashboard", view)
        self.assertLess(seconds, browser_tools.RACE_AFTER + 6)
        self.assertFalse(own)
        self.assertTrue(ok_event(session)["raced"])

    def test_a_page_that_stops_answering_is_given_up(self):
        """A page whose script calls never return (a hung renderer) ends its tab after STUCK_SECONDS; with no race
        the next attempt, in a fresh context, opens the site."""
        server, counts = serve(slow_home=False, slow_login=False)
        scaled(RACE_AFTER=60)

        async def broken(context):
            async def busy(route):
                await route.fulfill(content_type="text/html", body="<h1>Portal home</h1><script>setTimeout(() => "
                                    "{ while (true) {} }, 50)</script>")  # frozen before the page is settled
            await context.route("**/*", busy)

        session, view, seconds, own = asyncio.run(start(server, broken))
        server.shutdown()
        self.assertIn("Portal home", view)
        errors = [f.get("error", "") for _, kind, f in session.trace.events if kind == "start_failed"]
        self.assertEqual(len(errors), 1)
        self.assertIn("does not respond", errors[0])
        self.assertLess(seconds, 3 * browser_tools.STUCK_SECONDS + 6)
        self.assertFalse(own)
        self.assertEqual(ok_event(session)["attempt"], 2)
        self.assertTrue(ok_event(session)["fresh_context"])

    def test_a_rendered_page_that_never_finishes_loading_is_usable(self):
        """Settling never completes (the response never ends), but the page shows its content: that is a start."""
        server = serve_streaming()
        scaled(RACE_AFTER=60)

        session, view, seconds, own = asyncio.run(start(server))
        server.shutdown()
        self.assertIn("Portal home", view)
        self.assertLess(seconds, browser_tools.GOTO_SECONDS + browser_tools.SETTLE_SECONDS
                        + browser_tools.LOOK_SECONDS + 2)
        self.assertTrue(own)
        self.assertNotIn("start_failed", session.trace.kinds())

    def test_a_slow_first_tab_that_answers_first_keeps_its_tab(self):
        """A site slow for every tab (under load): the racer queues as well, the first tab wins and stays."""
        server, counts = serve(slow_home=True, slow_login=False)
        scaled()

        session, view, seconds, own = asyncio.run(start(server))
        server.shutdown()
        self.assertIn("Portal home", view)
        self.assertGreaterEqual(seconds, STALL - 0.5)
        self.assertTrue(own)
        self.assertFalse(ok_event(session)["raced"])
        self.assertEqual(counts["GET /"], 2)  # the racer's request; it was closed when the first tab won
        self.assertNotIn("start_failed", session.trace.kinds())

    def test_dialogs_of_the_winning_tab_report_to_the_session(self):
        """The winning tab belonged to a contender: its handlers must report to the session that took it over."""
        server, _ = serve(slow_home=False, slow_login=False)
        scaled()

        async def run():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context()
                await context.route("**/*", hang)  # so the racer's tab wins
                session = SiteSession(site(server), context, Recorder())
                await session.start(until=time.monotonic() + 30)
                self.assertTrue(ok_event(session)["raced"])
                await session.page.evaluate("setTimeout(() => alert('hello there'), 0)")
                await asyncio.sleep(0.5)
                view = await session.look()
                await browser.close()
                return view

        view = asyncio.run(run())
        server.shutdown()
        self.assertIn("hello there", view)


if __name__ == "__main__":
    unittest.main()
