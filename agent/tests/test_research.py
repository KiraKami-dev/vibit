"""The paging and multi-page tools and the research plumbing, on invented pages in a local Chromium."""
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import research  # noqa: E402
from browser_tools import Deps, SiteSession, _new_lines, _paged_part, read_all_pages, read_pages  # noqa: E402
from evidence import Requirement, TenderPosition  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from tracing import Trace  # noqa: E402

SITE = "http://shop.test"
ROWS = {1: "Item A-1 | 11 pcs", 2: "Item B-2 | 22 pcs", 3: "Item C-3 | 33 pcs"}


def list_page(number: int) -> str:
    more = f'<a href="/list?page={number + 1}">Next</a>' if number < 3 else ""
    return f"<h1>Orders</h1><p>{ROWS[number]}</p><p>Page {number} of 3</p>{more}"


def framed_page(number: int) -> str:
    # past the end the site repeats its last page, and the control stays
    shown = min(number, 3)
    return f'<p>Answer {ROWS[shown]}</p><a href="/inner?page={number + 1}">More answers</a>'


async def route(request_route):
    url = request_route.request.url
    page = int(url.split("page=")[1]) if "page=" in url else 1
    if "/list" in url:
        body = list_page(page)
    elif "/inner" in url:
        body = framed_page(page)
    elif "/outer" in url:
        body = '<h1>Questions</h1><a href="/list">Other</a><iframe src="/inner?page=1"></iframe><p>Footer 99</p>'
    else:
        body = f"<h1>Record</h1><p>Detail {url.rsplit('/', 1)[1]} holds 7{url[-1]} units</p><a href='/list'>Back</a>"
    await request_route.fulfill(status=200, content_type="text/html", body=body)


class PagingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        async def run():
            trace = Trace(tempfile.mkdtemp())
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context()
                await context.route("**/*", route)
                out = {}
                for name, start in (("list", "/list"), ("framed", "/outer")):
                    session = SiteSession({"siteId": "shop", "url": SITE + start, "credentials": {}}, context, trace)
                    view = await session.start()
                    label = "Next" if name == "list" else "More answers"
                    ref = view.split(f' link "{label}"')[0].rsplit("[", 1)[1]
                    ctx = SimpleNamespace(deps=Deps(session=session))
                    out[name] = (await read_all_pages(ctx, ref), session.seen)
                session = SiteSession({"siteId": "shop", "url": SITE + "/list", "credentials": {}}, context, trace)
                await session.start()
                ctx = SimpleNamespace(deps=Deps(session=session))
                out["several"] = (await read_pages(ctx, ["/record/x1", "/record/x2", "http://other.test/"]),
                                  session.seen)
                out["missing"] = (await read_all_pages(ctx, "e999"), "")
                await browser.close()
            return out
        cls.out = asyncio.run(run())

    def test_all_pages_of_a_list_come_back_as_one_view(self):
        text, seen = self.out["list"]
        for row in ROWS.values():
            self.assertIn(row, text)
            self.assertIn(row, seen)  # the grounding check accepts values from every page
        self.assertIn("Read 3 page(s)", text)
        self.assertIn("===== page 3 (open now)", text)
        self.assertIn("this was the last page", text)
        self.assertEqual(text.count("# Orders"), 1)  # the repeated top of the page is shown once

    def test_paging_inside_a_frame_stops_when_the_content_repeats(self):
        text, seen = self.out["framed"]
        for row in ROWS.values():
            self.assertIn(f"Answer {row}", text)
            self.assertIn(row, seen)
        self.assertIn("Read 3 page(s)", text)
        self.assertIn("came again", text)
        self.assertNotIn("Footer 99", text)  # only the paged document is shown
        self.assertEqual(text.count('[link "More answers"'), 2)  # pages no longer open carry no refs
        self.assertRegex(text, r'page 3 \(open now\) =====\n.*\n.*\n\[e\d+ link "More answers"')

    def test_several_pages_are_read_in_one_step(self):
        text, seen = self.out["several"]
        self.assertIn("Detail x1 holds 71 units", text)
        self.assertIn("Detail x2 holds 72 units", text)
        self.assertIn("71 units", seen)
        self.assertIn("not on this website", text)
        self.assertIn("Opened 2 of 3 pages", text)

    def test_an_unknown_ref_is_reported(self):
        self.assertIn("No link or button e999", self.out["missing"][0])


class PagingTextTest(unittest.TestCase):
    def test_the_paged_part_is_the_innermost_frame_around_the_control(self):
        view = 'Top\n[frame /a]\nOuter\n[frame /b]\nInner [e5 link "Next" -> /b?p=2]\n[/frame]\nAfter\n[/frame]\nEnd'
        part, framed = _paged_part(view, view.index("[e5"))
        self.assertTrue(framed)
        self.assertEqual(part, '[frame /b]\nInner [e5 link "Next" -> /b?p=2]\n[/frame]')
        self.assertEqual(_paged_part("Top [e1 link \"Next\" -> /x]", 4), ("Top [e1 link \"Next\" -> /x]", False))

    def test_only_new_lines_of_a_growing_list_are_shown(self):
        old = "Head\nTitle\nrow 1\n[e3 button \"More\"]"
        self.assertEqual(_new_lines("Head\nTitle\nrow 1\nrow 2\n[e4 button \"More\"]", old, True),
                         ('row 2\n[e4 button "More"]', 3))
        replaced = _new_lines("Head\nrow 9\nrow 8\nrow 7\nrow 6\nx", "Head\nrow 1\nrow 2\nrow 3\nrow 4\nx", True)
        self.assertEqual(replaced[1], 0)  # another page of the document is shown whole


class Session:
    """A stand-in for a signed-in tab: what the worker "saw" is given, nothing is opened."""

    def __init__(self, seen: str):
        self.seen, self.label, self.name = seen, "", "fake"


class ResearchPlumbingTest(unittest.TestCase):
    def make(self, parts=research.PARTS):
        briefing = SimpleNamespace(tender_reference="T-0001", scenario_date="2031-03-04", company_name="Example AG",
                                   tender_site="a", spec_site="b", erp_site="c", mail_site="d")
        updates = []
        run = research._Research(briefing, None, time.monotonic() + 60, Trace(tempfile.mkdtemp()), updates.append,
                                 parts)
        return run, updates

    def test_records_with_values_no_page_showed_are_refused(self):
        run, _ = self.make()
        session = Session("Position 10 | 25 pcs | REQ-7")
        good = TenderPosition(position="10", quantity="25", unit="pcs", requirement="REQ-7")
        invented = TenderPosition(position="20", quantity="99", unit="pcs", requirement="REQ-8")
        kept, problem = run.accept(session, [good, invented])
        self.assertEqual(kept, [good])
        self.assertIn("99", problem)
        self.assertIn("REQ-8", problem)

    def test_a_corrected_record_replaces_the_earlier_one(self):
        run, _ = self.make()
        target = [TenderPosition(position="10", quantity="25", unit="pcs")]
        run.merge(target, [TenderPosition(position="10", quantity="26", unit="pcs"),
                           TenderPosition(position="20", quantity="5", unit="set")], lambda p: p.position)
        self.assertEqual([(p.position, p.quantity) for p in target], [("10", "26"), ("20", "5")])

    def test_erp_lookups_start_for_referenced_requirements_once_positions_are_known(self):
        async def run_it():
            run, updates = self.make()
            started = []

            async def erp(requirement):
                started.append(requirement.requirement)
            run.erp = erp
            run.findings.spec.requirements = [Requirement(requirement="REQ-7", catalogue_group="G1"),
                                              Requirement(requirement="REQ-8", catalogue_group="G2"),
                                              Requirement(requirement="REQ-9")]
            run.findings.tender.positions = [TenderPosition(position="10", quantity="1", unit="pcs",
                                                            requirement="req 7")]
            run.updated()
            await asyncio.sleep(0)
            before = list(started)
            run.positions_done.set()
            run.updated()
            run.updated()
            await asyncio.gather(*run.tasks)
            return before, started, updates
        before, started, updates = asyncio.run(run_it())
        self.assertEqual(before, [])  # not before the tender's positions are in
        self.assertEqual(started, ["REQ-7"])  # the referenced one, once, written another way
        self.assertEqual(len(updates), 3)
        self.assertEqual(updates[-1].tender.positions[0].position, "10")

    def test_research_returns_partial_findings_at_the_deadline_and_never_raises(self):
        async def run_it():
            briefing = SimpleNamespace(tender_reference="T-0001", scenario_date="2031-03-04",
                                       company_name="Example AG", tender_site="a", spec_site="b", erp_site="c",
                                       mail_site="d")

            async def open_session(site_id):
                if site_id == "b":
                    raise RuntimeError("the tab did not open")
                await asyncio.sleep(60)

            updates = []
            started = time.monotonic()
            found = await research.research(briefing, open_session, started + research.MARGIN_SECONDS + 0.5,
                                            Trace(tempfile.mkdtemp()), updates.append)
            return found, time.monotonic() - started, updates
        found, seconds, updates = asyncio.run(run_it())
        self.assertLess(seconds, research.MARGIN_SECONDS + 0.6)
        self.assertEqual(found.tender.positions, [])
        self.assertFalse(found.spec.complete)
        self.assertTrue(updates)  # the caller holds the latest state before the cut-off


class OutputCapTest(unittest.TestCase):
    def test_every_call_has_an_output_cap_and_thinking_gets_more(self):
        plain, thinking = llm.settings(False), llm.settings(True)
        self.assertEqual(plain["max_tokens"], llm.MAX_TOKENS)
        self.assertGreater(thinking["max_tokens"], plain["max_tokens"])
        self.assertLessEqual(plain["timeout"], thinking["timeout"])


if __name__ == "__main__":
    unittest.main()
