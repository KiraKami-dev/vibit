"""
Starts on websites that are slow under load (a local server that stalls; local Chromium): a start waits on its tab
instead of throwing it away, a sign-in is not resent while its post is pending, a session handed out while its start
runs waits for it, and nothing else (the brief, workers on other websites) waits for a slow start. The server's
stalls and the time limits are scaled down by about ten (8 s stalls against 2-3 s step limits) to keep the suite fast.
"""
import asyncio
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import browser_tools  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

STALL = 8.0  # seconds the server holds a slow response (the sandbox's sites held them for minutes)
LOGIN = ("<form method=post action='/login'><input name=user><input name=pass type=password>"
         "<button>Sign in</button></form>")


class Recorder:
    """A Trace stand-in that keeps the events."""

    def __init__(self):
        self.events, self.last = [], 0.0

    def event(self, name, kind, quiet=False, **fields):
        self.events.append((name, kind, fields))

    def kinds(self, name=None):
        return [kind for n, kind, _ in self.events if name is None or n == name]


def serve(slow_home: bool, slow_login: bool):
    """A local site: `/` is the home page (stalled with slow_home) or, with slow_login, a sign-in form whose post
    stalls before the dashboard. Returns (server, counts)."""
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
            if slow_login:
                self._send(f"<h1>Portal sign-in</h1>{LOGIN}")
                return
            if slow_home:
                time.sleep(STALL)
            self._send("<h1>Portal home</h1><a href='/x'>Open tenders</a>")

        def do_POST(self):
            counts["POST /login"] += 1
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            time.sleep(STALL)
            self._send("<h1>Welcome dashboard</h1><a href='/x'>Open tenders</a>")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, counts


def site(server, name="portal") -> dict:
    return {"siteId": name, "url": f"http://127.0.0.1:{server.server_address[1]}/",
            "credentials": {"username": "bidder", "password": "secret"}}


SCALED = dict(GOTO_SECONDS=2, SETTLE_SECONDS=3, LOOK_SECONDS=3, START_SECONDS=25, START_BACKOFF=(0.2,),
              MIN_ATTEMPT=0.5, DEADLINE=None)


def scaled(**over):
    patches = [mock.patch.object(browser_tools, k, v) for k, v in {**SCALED, **over}.items()]
    for p in patches:
        p.start()
    return patches


async def browser_context(playwright):
    browser = await playwright.chromium.launch(headless=True)
    return browser, await browser.new_context()


class SlowStartTest(unittest.TestCase):
    def tearDown(self):
        mock.patch.stopall()

    def test_a_stalled_page_loads_on_the_same_tab(self):
        server, counts = serve(slow_home=True, slow_login=False)
        scaled()

        async def run():
            async with async_playwright() as playwright:
                browser, context = await browser_context(playwright)
                session = SiteSession(site(server), context, Recorder())
                began = time.monotonic()
                view = await session.start(until=time.monotonic() + 60)
                seconds = time.monotonic() - began
                await browser.close()
                return session, view, seconds

        session, view, seconds = asyncio.run(run())
        server.shutdown()
        self.assertIn("Portal home", view)
        self.assertGreaterEqual(seconds, STALL - 0.5)  # longer than the goto and settle limits together
        self.assertEqual(counts["GET /"], 1)  # one tab, one request: no reopen
        self.assertNotIn("start_failed", session.trace.kinds())
        self.assertIn("start_wait", session.trace.kinds())
        self.assertTrue(session.ok)

    def test_the_old_regime_would_have_failed(self):
        """With a per-attempt cap shorter than the stall (the former 90 s against minutes), each attempt is lost:
        the reason one attempt now has a budget of its own and keeps its tab."""
        server, counts = serve(slow_home=True, slow_login=False)
        scaled(START_SECONDS=4)

        async def run():
            async with async_playwright() as playwright:
                browser, context = await browser_context(playwright)
                session = SiteSession(site(server), context, Recorder())
                try:
                    await session.start(attempts=2, until=time.monotonic() + 60)
                    failed = False
                except TimeoutError:
                    failed = True
                await browser.close()
                return failed

        self.assertTrue(asyncio.run(run()))
        server.shutdown()
        self.assertEqual(counts["GET /"], 2)

    def test_a_stalled_sign_in_is_waited_for_and_not_resent(self):
        server, counts = serve(slow_home=False, slow_login=True)
        scaled()

        async def run():
            async with async_playwright() as playwright:
                browser, context = await browser_context(playwright)
                session = SiteSession(site(server), context, Recorder())
                view = await session.start(until=time.monotonic() + 60)
                await browser.close()
                return session, view

        session, view = asyncio.run(run())
        server.shutdown()
        self.assertIn("Welcome dashboard", view)
        self.assertEqual(counts["POST /login"], 1)
        self.assertEqual(counts["GET /"], 1)
        self.assertNotIn("start_failed", session.trace.kinds())

    def test_a_session_handed_out_while_starting_waits_for_its_start(self):
        server, counts = serve(slow_home=True, slow_login=False)
        scaled()

        async def run():
            async with async_playwright() as playwright:
                browser, context = await browser_context(playwright)
                session = SiteSession(site(server), context, Recorder())
                start = asyncio.create_task(session.start(until=time.monotonic() + 60))
                await asyncio.sleep(0.5)
                self.assertTrue(session.starting)
                view = await session.look()  # a worker's first look on the handed-out session
                await start
                await browser.close()
                return session, view

        session, view = asyncio.run(run())
        server.shutdown()
        self.assertIn("Portal home", view)
        self.assertEqual(counts["GET /"], 1)  # no second tab opened by revive
        self.assertIn("helper", session.trace.kinds())

    def test_a_fresh_context_after_two_failed_attempts(self):
        server, _ = serve(slow_home=False, slow_login=False)
        port = server.server_address[1]
        server.shutdown()
        server.server_close()  # nothing listens: the browser shows its error page at once
        scaled()

        async def run():
            async with async_playwright() as playwright:
                browser, context = await browser_context(playwright)
                session = SiteSession({"siteId": "gone", "url": f"http://127.0.0.1:{port}/",
                                       "credentials": {"username": "u", "password": "p"}}, context, Recorder())
                with self.assertRaises(TimeoutError):
                    await session.start(attempts=3, until=time.monotonic() + 60)
                same = session.context is context
                await browser.close()
                return session, same

        session, same = asyncio.run(run())
        actions = [f.get("action") for _, kind, f in session.trace.events if kind == "helper"]
        self.assertEqual(session.trace.kinds().count("start_failed"), 3)
        self.assertIn("fresh_context", actions)
        self.assertTrue(same)  # the fresh context did not help: back to the run's own
        self.assertFalse(session.ok)


class NoBlockingTest(unittest.TestCase):
    def tearDown(self):
        mock.patch.stopall()

    def test_brief_and_workers_do_not_wait_for_a_slow_start(self):
        slow, slow_counts = serve(slow_home=True, slow_login=False)
        fast, _ = serve(slow_home=False, slow_login=False)
        scaled()

        async def run():
            async with async_playwright() as playwright:
                browser, context = await browser_context(playwright)
                trace = Recorder()
                sessions = {"bid": SiteSession(site(slow, "bid"), context, trace),
                            "erp": SiteSession(site(fast, "erp"), context, trace)}
                began = time.monotonic()
                starts = agent.warm_up(sessions, time.monotonic() + 400, {"bid"})
                handed = time.monotonic() - began
                brief_done = None

                async def brief():  # the model call on the brief
                    nonlocal brief_done
                    await asyncio.sleep(0.2)
                    brief_done = time.monotonic() - began

                planner = SimpleNamespace(warm=sessions, trace=trace, left=lambda: 60.0)
                joined = {}

                async def worker(name):
                    await Orchestrator.site_up(planner, name)
                    joined[name] = time.monotonic() - began

                await asyncio.gather(brief(), worker("erp"), worker("bid"))
                await asyncio.gather(*starts.values())
                await browser.close()
                return handed, brief_done, joined, sessions

        handed, brief_done, joined, sessions = asyncio.run(run())
        slow.shutdown()
        fast.shutdown()
        self.assertLess(handed, 0.5)  # warm_up returns at once
        self.assertLess(brief_done, 2.0)  # the brief does not wait for the slow bid site
        self.assertLess(joined["erp"], STALL - 2)  # a worker on the fast site starts while the bid site loads
        self.assertGreaterEqual(joined["bid"], STALL - 0.5)  # and one on the slow site joins when it is up
        self.assertTrue(all(s.ok for s in sessions.values()))
        self.assertEqual(slow_counts["GET /"], 1)


if __name__ == "__main__":
    unittest.main()
