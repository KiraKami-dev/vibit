"""
Complete Q&A reading and its merging, the Q&A re-check before a no_bid, and per-revision stroke reserves, on
invented pages and values (no browser, no model).
"""
import asyncio
import sys
import time
import unittest
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gaps  # noqa: E402
import orchestrator as orch  # noqa: E402
import research_mail as rm  # noqa: E402
import research_tender as rt  # noqa: E402
from board import Board  # noqa: E402
from evidence import MaterialRecord, Requirement, RequirementResearch  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from research import ErpResearch  # noqa: E402

SCENARIO = datetime(2030, 3, 1, 12, tzinfo=UTC)
WHEN = datetime(2030, 2, 1, 8, tzinfo=UTC)


class Events:
    def __init__(self):
        self.events = []

    def event(self, worker, kind, quiet=False, **data):
        self.events.append((kind, data))

    def kinds(self, kind):
        return [d for k, d in self.events if k == kind]


def qa_page(number: int, *answers: str, next_page: int | None = None, frame_loading: bool = False) -> str:
    """A list page as the extractor writes it: the outer page with the list in an embedded frame."""
    head = "Portal\n# Tender T-1\n[e1 link \"Back\" -> /]\n[frame /t/T-1/qa-frame]\n# Questions\n"
    if frame_loading:
        return head + "[frame /t/T-1/qa-list (separate)]\nFooter\n[/frame]\n[e2 link \"Ask\" -> /ask]\n"
    body = "".join(f"[img: {a}] | [e{3 + i} button \"Received\"] (tooltip: 2030-02-0{i + 1}T08:00:00Z) |\n"
                   f"- Answer {a} text\n|\n" for i, a in enumerate(answers))
    nav = f"Page {number}\n" + (f"[e9 link \"Next page\" -> /t/T-1/qa-list?page={next_page}]\n" if next_page else "")
    return head + "[frame /t/T-1/qa-list]\n" + body + nav + "[/frame]\nFooter\n[/frame]\n"


def standalone(number: int, *answers: str, next_page: int | None = None) -> str:
    """The same list page opened on its own (no outer page)."""
    body = "".join(f"[img: {a}] | [e{1 + i} button \"Received\"] (tooltip: 2030-02-0{i + 1}T08:00:00Z) |\n"
                   f"- Answer {a} text\n|\n" for i, a in enumerate(answers))
    nav = f"Page {number}\n" + (f"[e8 link \"Next page\" -> /t/T-1/qa-list?page={next_page}]\n" if next_page else "")
    return body + nav


class FakeSession:
    """Clicking the list's next control shows the next scripted view."""

    def __init__(self, views: list[str]):
        self.views, self.view, self.seen = views[1:], views[0], views[0]
        self.page = SimpleNamespace(url="https://tender.example.com/t/T-1?tab=qa", frames=[])
        self.trace, self.name = Events(), "tender:qa"

    async def act(self, kind, ref, fn):
        if self.views:
            self.view = self.views.pop(0)
            self.seen += "\n" + self.view
        return self.view

    async def look(self, *args, **kwargs):
        return self.view


def run(coro):
    return asyncio.run(coro)


class CollectPages(unittest.TestCase):
    def setUp(self):
        self.saved = rt._view_of, rt.PAGE_WAITS
        rt.PAGE_WAITS = 1

    def tearDown(self):
        rt._view_of, rt.PAGE_WAITS = self.saved

    def collect(self, session, opened: dict[str, str]):
        asked = []

        async def view_of(s, url):
            asked.append(url)
            return opened.get(url)

        rt._view_of = view_of
        collected = rt.Collected()
        tool = rt.paging_tool(collected, thorough=True)
        answer = run(tool(SimpleNamespace(deps=SimpleNamespace(session=session)), "e9"))
        return collected, asked, answer

    def test_a_page_that_did_not_render_is_opened_by_its_target_and_the_rest_followed(self):
        session = FakeSession([qa_page(1, "101", next_page=2), qa_page(2, "102", next_page=3),
                               qa_page(3, "", frame_loading=True)])
        base = "https://tender.example.com/t/T-1/qa-list?page="
        collected, asked, _ = self.collect(session, {
            base + "3": standalone(3, "103", next_page=4), base + "4": standalone(4, "104")})
        text = "\n".join(collected.pages)
        for answer in ("101", "102", "103", "104"):
            self.assertIn(f"Answer {answer} text", text)
        self.assertEqual(asked, [base + "3", base + "4"])
        self.assertTrue(collected.finished)

    def test_the_last_page_is_checked_once_and_not_kept_twice(self):
        session = FakeSession([qa_page(1, "101", next_page=2), qa_page(2, "102")])
        base = "https://tender.example.com/t/T-1/qa-list?page="
        collected, asked, _ = self.collect(session, {base + "2": standalone(2, "102")})
        self.assertEqual(len(collected.pages), 2)
        self.assertEqual(asked, [base + "2"])


    def test_other_lists_keep_the_plain_paging(self):
        session = FakeSession([qa_page(1, "101", next_page=2), qa_page(2, "", frame_loading=True)])
        asked = []

        async def view_of(s, url):
            asked.append(url)

        rt._view_of = view_of
        collected = rt.Collected()
        run(rt.paging_tool(collected)(SimpleNamespace(deps=SimpleNamespace(session=session)), "e9"))
        self.assertEqual((len(collected.pages), asked), (2, []))


class Reading(unittest.TestCase):
    def test_views_the_worker_opened_after_collecting_are_read_too(self):
        collected = rt.Collected(pages=[qa_page(1, "101", next_page=2)])
        seen = collected.pages[0]
        collected.mark = len(seen)
        seen += "\n" + standalone(2, "102")
        pages = collected.reading(seen)
        self.assertEqual(len(pages), 2)
        self.assertIn("Answer 102 text", pages[1])
        self.assertNotIn("Answer 101 text", pages[1])

    def test_nothing_is_added_when_the_worker_only_revisited_kept_pages(self):
        collected = rt.Collected(pages=[qa_page(1, "101")])
        collected.mark = 0
        self.assertEqual(collected.reading(collected.pages[0] + "\n" + collected.pages[0]), collected.pages)

    def test_without_collect_pages_everything_seen_is_read(self):
        self.assertEqual(rt.Collected().reading("all views"), ["all views"])


def raw(number: str, requirement: str, quantity: str = "10") -> rt.TenderPosition:
    return rt.TenderPosition(position=number, short_text="Cylinder", quantity=quantity, unit="Stk",
                             requirement=requirement)


def amendment(number: str, value: str, day: int = 1) -> rt.QaAmendment:
    return rt.QaAmendment(answer_number=number, date=datetime(2030, 2, day, 8, tzinfo=UTC), revision="R-1",
                          requirement="A-1", attribute="stroke", attribute_label="Stroke", value=value, unit="mm")


class ReaderCompleteness(unittest.TestCase):
    def setUp(self):
        self.saved = rt._read

    def tearDown(self):
        rt._read = self.saved

    def test_answers_without_records_are_read_again(self):
        calls = []

        async def read(output_type, instructions, text, prompt, thinking=True):
            calls.append(prompt)
            if len(calls) == 1:  # the first reading skips answer 3
                return rt.QaPage(answers=["1", "2", "3"], withdrawals=[
                    rt.QaWithdrawal(answer_number="1", date=WHEN, withdrawn_number="2")],
                    packaging_tariffs=[rt.QaKey(answer_number="2", date=WHEN, position="1", key="P-1")])
            return rt.QaPage(answers=["1", "2", "3"], packaging_tariffs=[
                rt.QaKey(answer_number="3", date=WHEN, position="2", key="P-2")])

        rt._read = read
        qa = run(rt.read_qa(["view"], "T-1"))
        self.assertEqual(len(calls), 2)
        self.assertIn("3", calls[1])
        self.assertEqual(sorted(r.key for r in qa.packaging_tariffs), ["P-1", "P-2"])
        self.assertEqual(len(qa.withdrawals), 1)

    def test_a_complete_reading_is_not_repeated(self):
        calls = []

        async def read(output_type, instructions, text, prompt, thinking=True):
            calls.append(prompt)
            return rt.QaPage(answers=["2"], packaging_tariffs=[
                rt.QaKey(answer_number="Nr. 2", date=WHEN, position="1", key="P-1")])

        rt._read = read
        run(rt.read_qa(["view"], "T-1"))
        self.assertEqual(len(calls), 1)

    def test_a_load_more_view_replaces_the_shorter_one_although_its_button_moved(self):
        collected = rt.Collected()
        collected.add("[img: 1] text one\n[e5 button \"Load more\"]")
        collected.add("[img: 1] text one\n[img: 2] text two\n[e7 button \"Load more\"]")
        collected.add("[img: 1] text one\n[img: 2] text two\n[img: 3] text three")
        self.assertEqual(len(collected.pages), 1)
        self.assertIn("text three", collected.pages[0])


class Combine(unittest.TestCase):
    def test_positions_from_one_website_and_q_and_a_from_another(self):
        positions = rt.resolve_tender([raw("1", "A-1"), raw("2", "A-2")], rt.QaRecords(), SCENARIO)
        qa = rt.QaRecords(quantity_changes=[rt.QaQuantityChange(answer_number="7", date=WHEN, position="2",
                                                                quantity="12")],
                          amendments=[amendment("8", "90")])
        qa_only = rt.resolve_tender([], qa, SCENARIO)
        self.assertEqual(qa_only.positions, [])
        both = rt.combine([positions, qa_only], SCENARIO)
        self.assertEqual([p.quantity for p in both.positions], [10, 12])
        self.assertEqual([a.attributes for a in both.amendments], [{"stroke": D("90")}])
        self.assertEqual(rt.combine([qa_only, positions], SCENARIO).positions, both.positions)

    def test_an_answer_read_before_keeps_its_reading_and_new_answers_join(self):
        first = rt.resolve_tender([raw("1", "A-1")], rt.QaRecords(amendments=[amendment("8", "90"),
                                                                              amendment("7", "80")]), SCENARIO)
        second = rt.resolve_tender([], rt.QaRecords(amendments=[amendment("8", "95"), amendment("9", "85", 2)]),
                                   SCENARIO)
        merged = rt.combine([first, second], SCENARIO)
        self.assertEqual(sorted(str(a.attributes["stroke"]) for a in merged.amendments), ["80", "85", "90"])

    def test_a_complete_read_wins_over_an_incomplete_earlier_one(self):
        partial = rt.resolve_tender([raw("1", "A-1")], rt.QaRecords(amendments=[amendment("8", "95")]), SCENARIO)
        partial.qa_complete = False
        complete = rt.resolve_tender([], rt.QaRecords(amendments=[amendment("8", "90")]), SCENARIO)
        merged = rt.combine([partial, complete], SCENARIO)
        self.assertEqual([str(a.attributes["stroke"]) for a in merged.amendments], ["90"])
        self.assertEqual([p.position for p in merged.positions], ["1"])

    def test_the_later_dated_answer_wins_per_key_across_reads(self):
        first = rt.resolve_tender([raw("1", "A-1")], rt.QaRecords(packaging_tariffs=[
            rt.QaKey(answer_number="5", date=datetime(2030, 2, 1, tzinfo=UTC), position="1", key="P-OLD")]),
            SCENARIO)
        second = rt.resolve_tender([], rt.QaRecords(packaging_tariffs=[
            rt.QaKey(answer_number="6", date=datetime(2030, 2, 3, tzinfo=UTC), position="1", key="P-NEW")]),
            SCENARIO)
        self.assertEqual(rt.combine([first, second], SCENARIO).positions[0].packaging_key, "P-NEW")
        self.assertEqual(rt.combine([second, first], SCENARIO).positions[0].packaging_key, "P-NEW")

    def test_q_and_a_counts_as_complete_when_one_read_finished_it(self):
        early = rt.resolve_tender([raw("1", "A-1")], rt.QaRecords(), SCENARIO)
        early.qa_complete = False
        again = rt.resolve_tender([], rt.QaRecords(), SCENARIO)
        self.assertFalse(rt.combine([early], SCENARIO).qa_complete)
        self.assertTrue(rt.combine([early, again], SCENARIO).qa_complete)


def bare(trace=None) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    o.board, o.trace = Board(), trace or Events()
    o.reference, o.scenario, o.scenario_stamp = "T-1", SCENARIO, SCENARIO
    o.mail_facts, o.tender_facts, o.tender_reads, o.tender_calls = None, None, {}, 0
    o.plan = SimpleNamespace(needs=[SimpleNamespace(kind="tender", site="tender")])
    o.deadline = time.monotonic() + 1000
    o.research_deadline = o.deadline - 300
    o.chosen = {}
    return o


class TenderReads(unittest.TestCase):
    def test_two_websites_no_longer_overwrite_each_other(self):
        o = bare()
        session = SimpleNamespace(page=SimpleNamespace(url="https://tender.example.com/"), seen="")
        positions = rt.resolve_tender([raw("1", "A-1")], rt.QaRecords(), SCENARIO)
        qa_only = rt.resolve_tender([], rt.QaRecords(amendments=[amendment("8", "90")]), SCENARIO)
        o._tender_read("a#1", positions, "a", session)
        o._tender_read("b#2", qa_only, "b", session)
        self.assertEqual([p.position for p in o.tender_facts.positions], ["1"])
        self.assertEqual(len(o.tender_facts.amendments), 1)


class NoBidRecheck(unittest.TestCase):
    def setUp(self):
        self.saved = orch.reselect

    def tearDown(self):
        orch.reselect = self.saved

    def orchestrator(self, found: list[str]):
        o = bare()
        o.requirements = {"A-1": Requirement(requirement_id="A-1", revision="R-1", bore=D("50"), stroke=D("100"),
                                             pressure=D("160"), mounting="Flange", certification="None",
                                             catalog_group="G-1")}
        o.tender_facts = rt.resolve_tender([raw("1", "A-1")], rt.QaRecords(), SCENARIO)
        material = MaterialRecord(material_number="M-1", active=True, bore=D("50"), stroke=D("90"),
                                  pressure=D("160"), mounting="Flange", certification="None")
        o.erp = ErpResearch(requirements=[RequirementResearch(requirement_id="A-1", inspected=["M-1"])],
                            materials={"M-1": material})
        o.hints = []

        async def recheck_qa(hint, until):
            o.hints.append(hint)
            if found:
                o.tender_facts = rt.resolve_tender([raw("1", "A-1")],
                                                   rt.QaRecords(amendments=[amendment("8", "90")]), SCENARIO)
            return found

        async def reselect(reqs, erp, trace):
            req = reqs[0]
            return {req.requirement_id: "M-1" if req.stroke == D("90") else None}

        o.recheck_qa = recheck_qa
        orch.reselect = reselect
        o._calculate = lambda: ("recalculated", {}, [])
        return o

    def test_a_missed_amendment_found_by_the_recheck_changes_the_choice(self):
        o = self.orchestrator(["R-1/A-1: stroke=90"])
        self.assertEqual(run(o.recheck_no_bid(["1"])), ("recalculated", {}, []))
        self.assertEqual(o.chosen, {"A-1": "M-1"})
        self.assertIn("A-1 (stroke)", o.hints[0])

    def test_the_no_bid_stands_when_the_recheck_finds_nothing_new(self):
        o = self.orchestrator([])
        self.assertIsNone(run(o.recheck_no_bid(["1"])))
        self.assertEqual(o.chosen, {})
        self.assertTrue(o.trace.kinds("no_bid_recheck_done"))

    def test_no_recheck_without_time(self):
        o = self.orchestrator(["R-1/A-1: stroke=90"])
        o.deadline = time.monotonic() + orch.FORM_SECONDS + 30
        self.assertIsNone(run(o.recheck_no_bid(["1"])))
        self.assertEqual(o.hints, [])


class QaGap(unittest.TestCase):
    def test_an_unfinished_q_and_a_is_a_gap(self):
        facts = rt.resolve_tender([raw("1", "A-1")], rt.QaRecords(), SCENARIO)
        facts.qa_complete = False
        o = SimpleNamespace(plan=SimpleNamespace(needs=[SimpleNamespace(kind="tender", site="t")]),
                            tender_facts=facts, requirements={}, erp=None, chosen={},
                            mail_facts=SimpleNamespace(approval=object()), board=Board())
        self.assertIn("tender/qa", {g.ident for g in gaps.detect_gaps(o)})
        facts.qa_complete = True
        self.assertNotIn("tender/qa", {g.ident for g in gaps.detect_gaps(o)})


class ReservesPerRevision(unittest.TestCase):
    KIND = rm.Kind(name="end_reserves", subject="Release", sender="engineering",
                   sender_form="local_part_at_bidder_domain", site="mail", what="reserves",
                   schema=rm.SCHEMAS["end_reserves"])

    def record(self, day: int, *entries: tuple[str, str, str]) -> rm.MailRecord:
        return rm.MailRecord[rm.ReserveContent](
            subject="Release T-1", sender="engineering@example.com", recipient="sales@example.com",
            sent_at=datetime(2030, 2, day, tzinfo=UTC), reference="T-1", is_record=True, number_format="en",
            content=rm.ReserveContent(entries=[rm.EndReserve(key=k, revision=r, reserve_per_end=v)
                                               for k, r, v in entries]))

    def test_a_later_release_for_another_revision_keeps_ours(self):
        result = rm.KindResult(self.KIND, records=[self.record(1, ("K-1", "Rev. R-1", "4")),
                                                   self.record(2, ("K-1", "R-2", "7"))])
        facts = rm.resolve_mail([result], "T-1", "example.com", SCENARIO, "sales@example.com")
        self.assertEqual(len(facts.end_reserves), 2)
        o = bare()
        o.mail_facts = facts
        self.assertEqual(o._reserves("R-1"), {"K-1": D("4")})
        self.assertEqual(o._reserves("Revision R-2"), {"K-1": D("7")})

    def test_latest_per_key_and_revision_still_wins(self):
        result = rm.KindResult(self.KIND, records=[self.record(1, ("K-1", "R-1", "4")),
                                                   self.record(3, ("K-1", "R-1", "5"))])
        o = bare()
        o.mail_facts = rm.resolve_mail([result], "T-1", "example.com", SCENARIO, "sales@example.com")
        self.assertEqual(o._reserves("R-1"), {"K-1": D("5")})

    def test_a_reserve_without_revision_counts_after_our_own(self):
        o = bare()
        o.mail_facts = SimpleNamespace(end_reserves={
            "K-1": rm.Reserve(key="K-1", revision="", reserve_per_end_mm=D("3")),
            "K-1@r-1": rm.Reserve(key="K-1", revision="R-1", reserve_per_end_mm=D("4")),
            "K-2": rm.Reserve(key="K-2", revision="", reserve_per_end_mm=D("6"))})
        self.assertEqual(o._reserves("R-1"), {"K-1": D("4"), "K-2": D("6")})
        self.assertEqual(o._reserves("R-9"), {"K-1": D("3"), "K-2": D("6")})


if __name__ == "__main__":
    unittest.main()
