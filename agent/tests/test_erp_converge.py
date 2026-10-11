"""
ERP research converges: the search tool says when a hit list is complete, resends a search a pop-up swallowed and
records the site's no-hits notice for an empty group; a worker that has what it came for is ended; open_pages says
when nothing is left to open; hits whose rows fit are inspected first (up to 20), and the others are not waited for
once one of them is chosen. Pages, groups and materials are invented.
"""
import asyncio
import re
import sys
import threading
import unittest
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import research  # noqa: E402
import site_tools  # noqa: E402
from browser_tools import Deps, SiteSession  # noqa: E402
from evidence import Candidate, EffectiveRequirement, MaterialRecord, group_without_hits  # noqa: E402
from tests.test_site_tools import _Browser  # noqa: E402

FOOTER = "<footer><p>Demo system</p><p>System date 01.02.2030</p></footer>"

# A search form that lists the hits of a group; with `swallow`, its first submit loses the query (as when a pop-up
# re-renders the form) and shows the empty result page.
SEARCH = """
<form onsubmit="go(); return false;">
<label>Term <input id="q" name="q"></label> <button type="submit">Suchen</button>
</form>
<p id="count">0 Treffer</p>
<table><tbody id="out"></tbody></table>
""" + FOOTER + """
<script>
let swallow = %s;
function go() {
  const q = document.getElementById('q').value;
  const out = document.getElementById('out');
  if (swallow) { swallow = false; document.getElementById('q').value = ''; out.innerHTML = ''; return; }
  const hits = {'GR-1': ['M-11'], 'GR-2': ['M-21', 'M-22']}[q] || [];
  document.getElementById('count').textContent = hits.length + ' Treffer';
  out.innerHTML = hits.map(h => `<tr><td><a href="/m/${h}">${h}</a></td><td>Zylinder 50/100, ${q}</td></tr>`).join('');
}
</script>
"""


def search(swallow: bool, query: str) -> tuple[str, list]:
    async def go():
        async with _Browser() as b:
            session = await b.session(SEARCH % ("true" if swallow else "false"))
            view = await session.look()
            ref = re.search(r'\[(e\d+) textbox "Term"', view).group(1)
            out = await site_tools.run_search(session, ref, query)
            return out, session.__dict__.get("searches", [])
    return asyncio.run(go())


class SearchToolTest(unittest.TestCase):
    def test_single_hit_is_reported_complete(self):
        out, searches = search(False, "GR-1")
        self.assertIn("1 rows", out)
        self.assertIn("This is the complete result list: 1 hit, no further page.", out)
        self.assertTrue(searches[0]["complete"])
        self.assertIs(research.group_listing(searches, "GR-1"), searches[0])
        self.assertFalse(group_without_hits(searches, "GR-1"))

    def test_swallowed_query_is_sent_again(self):
        out, searches = search(True, "GR-2")
        self.assertIn("2 rows", out)
        self.assertIn("complete result list: 2 hits", out)
        self.assertEqual(len(searches), 1)

    def test_empty_group_shows_no_chrome_rows_and_is_flagged(self):
        out, searches = search(False, "GR-9")
        self.assertIn("(no rows)", out)
        self.assertNotIn("Demo system", out)
        self.assertNotIn("complete result list", out)
        self.assertTrue(searches[0]["no_hits"])
        self.assertIsNone(research.group_listing(searches, "GR-9"))
        self.assertTrue(group_without_hits(searches * 2, "GR-9"))


class UntilSettledTest(unittest.TestCase):
    def test_worker_that_keeps_going_is_ended(self):
        trace = SimpleNamespace(events=[], event=lambda *a, **k: trace.events.append(a[1]))
        cancelled = []

        async def forever():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        async def go():
            loop = asyncio.get_running_loop()
            at = loop.time()
            return await research.until_settled(forever(), lambda: at, 0.2, trace, "w")
        result, ended = asyncio.run(go())
        self.assertEqual((result, ended), (None, True))
        self.assertEqual(cancelled, [True])
        self.assertIn("settled_stop", trace.events)

    def test_worker_that_returns_keeps_its_result(self):
        async def quick():
            await asyncio.sleep(0.05)
            return "report"

        trace = SimpleNamespace(event=lambda *a, **k: None)
        self.assertEqual(asyncio.run(research.until_settled(quick(), lambda: 0.0, 60, trace, "w")),
                         ("report", False))


def req(stroke: str | None = "100") -> EffectiveRequirement:
    return EffectiveRequirement(requirement_id="A-1", catalog_group="GR-1", bore=D("50"),
                                stroke=D(stroke) if stroke else None, pressure=D("200"), mounting="Flansch",
                                certification="keine", pending=[] if stroke else ["reserve"])


def cand(number: str, bore: str | None = "50", mounting: str = "", stroke: str | None = None) -> Candidate:
    return Candidate(material_number=number, bore=D(bore) if bore else None, mounting=mounting,
                     stroke=D(stroke) if stroke else None)


class InspectionOrderTest(unittest.TestCase):
    def test_fitting_rows_first_and_up_to_twenty(self):
        hits = [cand(f"M-{i:02}", bore="63") for i in range(15)] + [cand("M-90"), cand("M-91", mounting="Flansch")]
        hits += [cand(f"M-{i}", bore="80") for i in range(30, 40)]
        first, rest = research.inspection_order(req(), hits)
        self.assertEqual(first, ["M-90", "M-91"])
        self.assertEqual(len(first + rest), 20)
        self.assertEqual(rest[:2], ["M-00", "M-01"])

    def test_unshown_and_pending_attributes_contradict_nothing(self):
        self.assertEqual(research.contradicted(req(None), cand("M-1", bore=None, stroke="120")), [])
        self.assertEqual(research.contradicted(req(), cand("M-1", mounting="Schwenkauge")),
                         ["mounting schwenkauge instead of flansch"])

    def test_few_hits_are_all_inspected(self):
        first, rest = research.inspection_order(req(), [cand("M-1"), cand("M-2", bore="63"), cand("M-1")])
        self.assertEqual((first, rest), (["M-1"], ["M-2"]))


def record(number: str) -> MaterialRecord:
    return MaterialRecord(material_number=number, active=True, status="01", bore=D("50"), stroke=D("100"),
                          pressure=D("200"), mounting="Flansch", certification="keine")


class RequirementPhasesTest(unittest.TestCase):
    """Erp.requirement with its browser parts replaced: the search result and the material workers are fakes."""

    def run_requirement(self, report, searches=(), slow=(), pick=lambda materials: sorted(materials)[0]):
        erp = research.Erp({"siteId": "erp", "url": "http://localhost/"}, None,
                           SimpleNamespace(event=lambda *a, **k: None), deadline=None)
        erp.left = lambda: 600.0

        class Slot:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *exc):
                return False

        erp._slot = Slot
        erp._worker_tab = lambda tag, url=None: asyncio.sleep(0, result=SimpleNamespace(name=tag))
        erp._close = lambda session: asyncio.sleep(0)

        async def fake_search(session, req_, until, recorded):
            recorded.extend(searches)
            return report
        erp._search = fake_search

        async def material(number, link=""):
            await asyncio.sleep(5 if number in slow else 0.01)
            return record(number)
        self.started, self.erp = [], erp

        def start(number, link=""):
            self.started.append(number)
            return asyncio.ensure_future(material(number, link))
        erp.material = start

        async def choose(req_, research_, materials, trace=None):
            return (pick(materials) if materials else None), []
        original = research.choose_material
        research.choose_material = choose
        try:
            async def go():
                return await asyncio.wait_for(erp.requirement(req()), 3)
            return asyncio.run(go())
        finally:
            research.choose_material = original

    def test_choice_among_fitting_rows_does_not_wait_for_the_others(self):
        report = research.SearchReport(search_term="GR-1", candidates=[
            research.CandidateSeen(material_number="M-2", link="/m/2", bore="63"),
            research.CandidateSeen(material_number="M-1", link="/m/1", bore="50")])
        out = self.run_requirement(report, slow={"M-2"})
        self.assertEqual(out.inspected, ["M-1", "M-2"])
        self.assertEqual(out.selected, "M-1")
        self.assertEqual(out.problems, [])

    def test_contradicted_rows_wait_until_needed(self):
        report = research.SearchReport(search_term="GR-1", candidates=[
            research.CandidateSeen(material_number="M-2", link="/m/2", bore="63"),
            research.CandidateSeen(material_number="M-1", link="/m/1", bore="50")])
        out = self.run_requirement(report)
        self.assertEqual(out.selected, "M-1")
        self.assertEqual(self.started, ["M-1"])
        self.assertEqual(self.erp.deferred, {"A-1": ["M-2"]})
        self.assertEqual(out.inspected, ["M-1", "M-2"])  # still listed: undecided, never "no material"

    def test_deferred_hits_are_inspected_when_they_fit_the_final_requirement(self):
        report = research.SearchReport(search_term="GR-1", candidates=[
            research.CandidateSeen(material_number="M-2", link="/m/2", bore="63"),
            research.CandidateSeen(material_number="M-1", link="/m/1", bore="50")])
        self.run_requirement(report)
        amended = req().model_copy(update={"bore": D("63")})
        found = asyncio.run(self.erp.inspect_deferred(amended, 3))
        self.assertEqual(sorted(found), ["M-2"])
        self.assertEqual(self.erp.deferred, {})

    def test_deferred_hits_that_still_do_not_fit_stay_unread(self):
        report = research.SearchReport(search_term="GR-1", candidates=[
            research.CandidateSeen(material_number="M-2", link="/m/2", bore="63"),
            research.CandidateSeen(material_number="M-1", link="/m/1", bore="50")])
        self.run_requirement(report)
        self.assertEqual(asyncio.run(self.erp.inspect_deferred(req(), 3)), {})
        self.assertEqual(self.started, ["M-1"])

    def test_without_a_fitting_choice_every_hit_is_inspected(self):
        report = research.SearchReport(search_term="GR-1", candidates=[
            research.CandidateSeen(material_number="M-2", link="/m/2", bore="63"),
            research.CandidateSeen(material_number="M-1", link="/m/1", bore="50")])
        out = self.run_requirement(report, pick=lambda materials: None)
        self.assertEqual(sorted(self.started), ["M-1", "M-2"])
        self.assertEqual(self.erp.deferred, {})
        self.assertIsNone(out.selected)

    def test_empty_group_is_flagged(self):
        searches = [{"query": "GR-1", "rows": 0, "no_hits": True, "complete": False, "text": "(no rows)", "at": 0}]
        out = self.run_requirement(None, searches * 2)
        self.assertTrue(out.no_hits)
        self.assertEqual(out.candidates, [])
        self.assertNotIn("search worker returned no result", out.problems)

    def test_hits_clear_the_flag(self):
        searches = [{"query": "GR-1", "rows": 0, "no_hits": True, "complete": False, "text": "(no rows)", "at": 0}]
        report = research.SearchReport(search_term="GR 1", candidates=[
            research.CandidateSeen(material_number="M-1", link="/m/1")])
        self.assertFalse(self.run_requirement(report, searches * 2).no_hits)


class OpenPagesTest(unittest.TestCase):
    """open_pages on a local site: a record with two tabs, one of which links to a sub-view."""

    PAGES = {"/rec": "<h1>Record</h1><a href='/rec/a'>Tab A</a> <a href='/rec/b'>Tab B</a>",
             "/rec/a": "<h1>Tab A</h1><p>Value 1</p><a href='/rec/a/sub'>Details</a>",
             "/rec/b": "<h1>Tab B</h1><p>Value 2</p>",
             "/rec/a/sub": "<h1>Details</h1><p>Value 3</p>"}

    def test_done_signal_once_every_link_is_open(self):
        pages = self.PAGES

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                data = f"<!doctype html><html><body>{pages.get(self.path, 'missing')}</body></html>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"

        async def go():
            async with _Browser() as b:
                session = SiteSession({"siteId": "erp", "url": base + "/", "credentials": {}}, b.context,
                                      SimpleNamespace(event=lambda *a, **k: None))
                page = await b.context.new_page()
                session._watch(page)
                await page.goto(base + "/rec")
                await session.look()
                ctx = SimpleNamespace(deps=Deps(session=session))
                first = await research.open_pages(ctx, ["/rec/a", "/rec/b"])
                exhausted_after_first = session.__dict__.get("exhausted_at")
                second = await research.open_pages(ctx, ["/rec/a/sub"])
                return first, exhausted_after_first, second, session.__dict__.get("exhausted_at")
        try:
            first, early, second, late = asyncio.run(go())
        finally:
            server.shutdown()
        self.assertIn("not opened yet: /rec/a/sub", first)
        self.assertIsNone(early)
        self.assertIn("No unopened links remain", second)
        self.assertIsNotNone(late)


if __name__ == "__main__":
    unittest.main()
