"""
A form page that stops answering script calls mid-form (run ec4e: the bid form hung for 8 minutes and nothing was
signed): the session opens it again in a new tab, keeping the site's session and with it the wizard's step, or,
when that fails too, starts the site again in a fresh context. Local server and Chromium; time limits scaled down.
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

import browser_tools  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from submit import PositionDesk  # noqa: E402
from tests.test_slow_start import Recorder  # noqa: E402

HANG = "setTimeout(() => { for (;;) {} }, 0)"  # the page's script never returns again


def serve():
    """A wizard whose step lives on the server, per session cookie: GET /form shows it, POST /next advances."""
    steps: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _sid(self) -> str:
            cookie = self.headers.get("Cookie") or ""
            return cookie.split("sid=")[1].split(";")[0] if "sid=" in cookie else ""

        def _send(self, body: str, sid: str = "") -> None:
            data = f"<!doctype html><html><body>{body}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if sid:
                self.send_header("Set-Cookie", f"sid={sid}; Path=/")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            sid = self._sid() or f"s{len(steps) + 1}"
            steps.setdefault(sid, 1)
            if self.path.startswith("/form"):
                self._send(f"<h1>Bid form step {steps[sid]}</h1><form method=post action=/next>"
                           "<label>Menge <input name=q></label><button>Weiter</button></form>", sid)
            else:
                self._send("<h1>Portal</h1><a href='/form'>Bid</a>", sid)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            sid = self._sid()
            steps[sid] = steps.get(sid, 1) + 1
            self.send_response(303)
            self.send_header("Location", "/form")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


FAST = dict(STUCK_SECONDS=2, PROBE_SECONDS=1, GOTO_SECONDS=5, SETTLE_SECONDS=12)


class DeadPage(unittest.TestCase):
    def run_site(self, work):
        server = serve()
        url = f"http://127.0.0.1:{server.server_address[1]}/"

        async def run():
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context()
                    session = SiteSession({"siteId": "portal", "url": url, "credentials": {}}, context, Recorder())
                    with mock.patch.multiple(browser_tools, **FAST):
                        await session.start(attempts=1)
                        await session._goto(url + "form")
                        await session.page.click("text=Weiter")  # the wizard moves to step 2 on the server
                        await session.settle()
                        return await work(session)
                finally:
                    await browser.close()
        try:
            return asyncio.run(run())
        finally:
            server.shutdown()

    def test_a_hung_form_is_opened_again_where_it_stood(self):
        async def work(session):
            await session.page.evaluate(HANG)
            began = time.monotonic()
            view = await session.look()
            return view, time.monotonic() - began, session

        view, seconds, session = self.run_site(work)
        self.assertIn("Bid form step 2", view)  # same session: the server-side step is kept
        self.assertLess(seconds, 40)
        self.assertIn("reopen", [f.get("action") for _, kind, f in session.trace.events if kind == "helper"])
        self.assertEqual(session.restarts, 0)

    def test_an_action_on_a_hung_page_settles_into_a_new_tab(self):
        async def work(session):
            await session.page.evaluate(HANG)
            await session.settle()  # what every browser tool does after its action
            return await session._answers(), session.page.url

        answer, url = self.run_site(work)
        self.assertEqual(answer, "yes")
        self.assertTrue(url.endswith("/form"))

    def test_when_a_new_tab_fails_too_the_site_starts_again(self):
        async def work(session):
            await session.page.evaluate(HANG)
            broken = session.context

            async def no_tab():
                raise RuntimeError("context is broken")

            with mock.patch.object(broken, "new_page", no_tab):
                ok = await session.reopen("test")
            return ok, session.restarts, session.context is not broken, await session.look()

        ok, restarts, fresh, view = self.run_site(work)
        self.assertTrue(ok)
        self.assertEqual(restarts, 1)
        self.assertTrue(fresh)
        self.assertIn("Portal", view)


    def test_a_research_tool_waiting_on_a_hung_page_is_cut_off(self):
        import worker

        async def work(session):
            async def read_records():  # a helper reading a page whose script never returns (run d253: 578 s)
                return await session.page.evaluate("() => { for (;;) {} }")

            async def slow_but_alive():
                await asyncio.sleep(3 * browser_tools.STUCK_SECONDS)
                return "records read"

            began = time.monotonic()
            text = await worker.watch_page(session, read_records())
            seconds = time.monotonic() - began
            return text, seconds, await session._answers(), await worker.watch_page(session, slow_but_alive())

        text, seconds, answer, slow = self.run_site(work)
        self.assertIn("stopped answering", text)
        self.assertIn("Bid form step 2", text)  # opened again where it stood
        self.assertLess(seconds, 40)
        self.assertEqual(answer, "yes")
        self.assertEqual(slow, "records read")  # a slow page that answers is never cut off


class LostPositions(unittest.TestCase):
    def test_a_restart_forgets_what_was_entered(self):
        desk = PositionDesk({"1": {"position": "1", "material_number": "M", "quantity": 2, "unit_price": 1.5,
                                   "lead_time_weeks": 1}}, deadline=0)
        desk.entered.add("1")
        session = SimpleNamespace(restarts=0, name="portal", trace=Recorder())
        desk.sync(session)
        self.assertEqual(desk.entered, {"1"})
        session.restarts = 1
        desk.sync(session)
        self.assertEqual(desk.entered, set())
        self.assertEqual(desk.total(), 0)


if __name__ == "__main__":
    unittest.main()
