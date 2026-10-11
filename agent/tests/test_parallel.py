"""v3: the revision judged twice, each spec page read twice, rework only for doubted records, and the bid's
positions entered in one tool call with a guard against walking past one."""
import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import orchestrator
import research
from board import Board, Fact, Source
from research import CurrentRevision, RevisionHistory, RevisionSeen
from tracing import Trace

HISTORY = RevisionHistory(document="D-1", revisions=[RevisionSeen(revision="R-1", status="replaced"),
                                                     RevisionSeen(revision="R-2", status="valid")])


class RevisionJudgedTwice(unittest.TestCase):
    def choose(self, answers: list, judges: int = 2):
        calls = []

        async def judge(history, view, context, *, listing=True, opinions=""):
            calls.append({"listing": listing, "opinions": opinions})
            answer = answers[len(calls) - 1]
            return CurrentRevision(revision=answer, why=f"why {answer}") if answer else None

        with patch.object(research, "_judge_revision", judge), patch.object(research, "REVISION_JUDGES", judges):
            choice = asyncio.run(research._choose_revision(HISTORY, "page", "context"))
        return choice, calls

    def test_two_judges_that_agree_settle_it(self):
        choice, calls = self.choose(["R-2", "Revision R-2"])
        self.assertEqual(choice.revision, "R-2")
        self.assertEqual([c["listing"] for c in calls], [True, False])  # one reads the page alone

    def test_a_dispute_goes_to_a_third_call_that_sees_both(self):
        choice, calls = self.choose(["R-2", "R-1", "R-1"])
        self.assertEqual(choice.revision, "R-1")
        self.assertEqual(len(calls), 3)
        self.assertIn("R-2: why R-2", calls[2]["opinions"])
        self.assertIn("R-1: why R-1", calls[2]["opinions"])

    def test_one_failed_judge_leaves_the_other(self):
        self.assertEqual(self.choose([None, "R-2"])[0].revision, "R-2")
        self.assertEqual(self.choose(["R-2", None])[0].revision, "R-2")
        self.assertIsNone(self.choose([None, None])[0])

    def test_a_failed_arbiter_keeps_the_first_judgement(self):
        self.assertEqual(self.choose(["R-2", "R-1", None])[0].revision, "R-2")

    def test_one_judge_when_set_so(self):
        choice, calls = self.choose(["R-2"], judges=1)
        self.assertEqual((choice.revision, len(calls)), ("R-2", 1))


def bare() -> orchestrator.Orchestrator:
    o = object.__new__(orchestrator.Orchestrator)
    o.trace = Trace(tempfile.mkdtemp())
    o.board = Board(o.trace)
    o.rules, o.reference = "rules", "T-1"
    o.briefing = SimpleNamespace(scenario_date="2026-06-01")
    o.research_deadline = time.monotonic() + 600
    o.mail_deadline = time.monotonic() + 600
    o.task = {"brief": "Brief"}
    return o


def doubt(key: str, rework: bool) -> Fact:
    return Fact(kind="mail", key=key, value="v", source=Source(site="mail"), worker="mails", verdict="doubt",
                rework=rework)


class ReworkOnlyForRecordDoubts(unittest.TestCase):
    def verify(self, rounds: list[list[Fact]], on: bool = True):
        calls, reworked = [], []

        async def verify_board(board, rules, reference, scenario):
            calls.append(1)
            return rounds[len(calls) - 1]

        async def rework(facts):
            reworked.append([f.key for f in facts])

        o = bare()
        o.rework = {"mails": rework}
        with patch.object(orchestrator, "verify_board", verify_board), patch.object(orchestrator, "REWORK", on):
            asyncio.run(o.verify())
        return len(calls), reworked

    def test_a_quote_that_does_not_show_the_value_sends_nothing_back(self):
        self.assertEqual(self.verify([[doubt("a", False), doubt("b", False)]]), (1, []))

    def test_a_doubted_record_goes_back_once_and_is_judged_again(self):
        self.assertEqual(self.verify([[doubt("a", False), doubt("b", True)], []]), (2, [["b"]]))

    def test_no_doubt_is_one_check(self):
        self.assertEqual(self.verify([[]]), (1, []))

    def test_by_default_nothing_is_searched_again(self):
        self.assertFalse(orchestrator.REWORK)
        self.assertEqual(self.verify([[doubt("a", True), doubt("b", True)]], on=False), (1, []))


class MailboxTab(unittest.TestCase):
    """The TAN is fetched in a webmail tab the research used last, reloaded while the bid form is filled in."""

    def session(self, name: str, closed: bool = False, ok: bool = True):
        events: list[str] = []

        async def revive():
            events.append("revive")

        async def goto(url):
            events.append("goto")
        page = SimpleNamespace(url="https://mail.test/", is_closed=lambda: closed)
        return SimpleNamespace(name=name, ok=ok, page=page, lock=asyncio.Lock(), revive=revive, _goto=goto,
                               trace=Trace(tempfile.mkdtemp()), events=events)

    def adopt(self, sessions, busy: bool = False):
        import submit

        async def run():
            first = self.session("mail")
            box = submit.Mailbox(first, SimpleNamespace(tender_reference="T-1"), time.monotonic() + 600)
            if busy:
                await box.lock.acquire()
            box.adopt(sessions)
            if box.warming is not None:
                await box.warming
            return box, first
        return asyncio.run(run())

    def test_the_last_usable_research_tab_is_taken_and_reloaded(self):
        tabs = [self.session("mail:mail1"), self.session("mail:mail2"), self.session("mail:mail3", closed=True)]
        box, first = self.adopt(tabs)
        self.assertEqual(box.session.name, "mail:mail2")
        self.assertEqual((tabs[1].events, first.events), (["revive", "goto"], []))

    def test_without_research_tabs_its_own_tab_is_reloaded(self):
        box, first = self.adopt([])
        self.assertEqual((box.session.name, first.events), ("mail", ["revive", "goto"]))
        box, first = self.adopt([self.session("mail:mail1", ok=False)])
        self.assertEqual(box.session.name, "mail")

    def test_a_running_lookup_keeps_its_tab(self):
        box, first = self.adopt([self.session("mail:mail1")], busy=True)
        self.assertEqual((box.session.name, box.warming), ("mail", None))


def seen(rid: str, **changes) -> research.RequirementSeen:
    values = dict(requirement_id=rid, bore="50", stroke="100", pressure="160", mounting="Flansch",
                  certification="keine", catalog_group="G-1", pieces_per_set="5", stroke_is_usable_travel=False)
    values.update(changes)
    return research.RequirementSeen(**values)


def page(*requirements, revision: str = "R-2") -> research.SpecPage:
    return research.SpecPage(revision_shown=revision, requirements=list(requirements))


class SpecPageReadTwice(unittest.TestCase):
    def extract(self, readings: list, reads: int = 2):
        calls = []

        async def grounded_read(output_type, instructions, prompt, source, *, trace=None, name="", what="",
                                thinking=None):
            calls.append({"thinking": thinking, "prompt": prompt})
            answer = readings[len(calls) - 1]
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch.object(research, "_grounded_read", grounded_read), patch.object(research, "SPEC_READS", reads):
            out = asyncio.run(research._extract_page("text", "D-1", "R-2", "context", None, "spec"))
        return out, calls

    def test_two_readings_that_agree_stand(self):
        out, calls = self.extract([page(seen("A-1")), page(seen("A-1", mounting=" flansch "))])
        self.assertEqual((out.requirements[0].pieces_per_set, len(calls)), ("5", 2))

    def test_a_differing_value_goes_to_a_thinking_reader_that_is_told_what_differs(self):
        out, calls = self.extract([page(seen("A-1", pieces_per_set="1")), page(seen("A-1")), page(seen("A-1"))])
        self.assertEqual(out.requirements[0].pieces_per_set, "5")
        self.assertEqual([c["thinking"] for c in calls], [None, None, True])
        self.assertIn("A-1 pieces_per_set: '1' or '5'", calls[2]["prompt"])

    def test_a_locale_tag_on_plain_digits_is_no_difference(self):
        out, calls = self.extract([page(seen("A-1", stroke="100")), page(seen("A-1", stroke="100 (en-US)"))])
        self.assertEqual(len(calls), 2)
        differences = research._reading_differences(page(seen("A-1", bore="1.250")), page(seen("A-1", bore="1.250 (de-DE)")))
        self.assertEqual(len(differences), 1)  # with a separator the tag decides the number

    def test_a_requirement_only_one_reading_has_is_a_difference(self):
        differences = research._reading_differences(page(seen("A-1")), page(seen("A-1"), seen("A-2")))
        self.assertEqual(differences, ["requirement A-2 is in only one reading"])

    def test_one_failed_reading_leaves_the_other(self):
        out, calls = self.extract([ValueError("bad"), page(seen("A-1"))])
        self.assertEqual((out.requirements[0].requirement_id, len(calls)), ("A-1", 2))
        self.assertIsNone(self.extract([ValueError("bad"), ValueError("bad")])[0])

    def test_a_failed_third_reading_keeps_the_first(self):
        out, _ = self.extract([page(seen("A-1", bore="63")), page(seen("A-1")), ValueError("bad")])
        self.assertEqual(out.requirements[0].bore, "63")

    def test_one_reading_when_set_so(self):
        out, calls = self.extract([page(seen("A-1"))], reads=1)
        self.assertEqual(len(calls), 1)


POSITION_PAGE = """# Position {n}
Hydraulic cylinder
[e7 select "Bore (mm)"]
[e8 button "Omit position"]
[e9 button "Next"]"""
OVERVIEW = """# Tender
Positions 01.01, 01.02
[e7 button "Start the bid"]"""


class NoWalkingPastAPosition(unittest.TestCase):
    def desk(self):
        from submit import PositionDesk
        return PositionDesk({"01.01": {"position": "01.01"}, "01.02": {"position": "01.02"}}, time.monotonic() + 900)

    def test_a_page_of_a_position_with_values_is_named_once(self):
        desk = self.desk()
        hint = desk.premature(POSITION_PAGE.format(n="01.02"))
        self.assertIn("configure_position('01.02'", hint)
        self.assertIsNone(desk.premature(POSITION_PAGE.format(n="01.02")))  # never twice: it cannot block

    def test_entered_tried_and_foreign_positions_are_left_alone(self):
        desk = self.desk()
        desk.entered.add("01.01")
        desk.failures["01.02"] = 1
        self.assertIsNone(desk.premature(POSITION_PAGE.format(n="01.01")))
        self.assertIsNone(desk.premature(POSITION_PAGE.format(n="01.02")))
        self.assertIsNone(desk.premature(POSITION_PAGE.format(n="01.07")))

    def test_a_page_that_lists_several_positions_is_not_a_position_page(self):
        self.assertIsNone(self.desk().premature(OVERVIEW))

    def test_the_session_returns_the_hint_instead_of_acting(self):
        from browser_tools import SiteSession

        async def run():
            session = SiteSession({"siteId": "bid", "url": "https://bid.test"}, None, Trace(tempfile.mkdtemp()))
            session.view = POSITION_PAGE.format(n="01.01")
            desk = self.desk()
            session.guard = lambda kind, view: desk.premature(view)

            async def revive():
                return None

            async def look(*a, **k):
                return session.view
            session.revive, session.look = revive, look
            acted = []

            async def fn(element):
                acted.append(element)
            return await session.act("click", "e8", fn), acted

        out, acted = asyncio.run(run())
        self.assertIn("configure_position('01.01'", out)
        self.assertEqual(acted, [])


def position_page(number: str) -> str:
    return (f"# Position {number}\nCylinder\n[e3 select \"Bore (mm)\"]\n[e4 select \"Certification\"]\n"
            f"[e5 textbox \"Quantity\"]\n[e6 button \"Apply\"]\n[e7 button \"Omit position\"]\n[e8 button \"Save and next\"]")


TOTALS_PAGE = "# Totals\n[e3 textbox \"Discount\"]\n[e4 button \"Continue\"]"
PREVIEW_PAGE = "# Next up: position 01.03\n[e4 button \"Continue\"]"
ENTRY = {"material_number": "M-1", "quantity": 2, "unit_price": 10, "lead_time_weeks": 3, "bore": "50",
         "certification": "None"}
FIELDS = {"Bore (mm)": "bore", "Certification": "certification", "Quantity": "quantity"}


class FormPages:
    """A bid form as a list of page views: the save button moves to the next one."""

    def __init__(self, pages: list[str]):
        self.pages, self.at = pages, 0
        self.lock, self.page = asyncio.Lock(), SimpleNamespace(url="https://bid.test/form")
        self.trace, self.name, self.seen = Trace(tempfile.mkdtemp()), "bid", ""

    async def look(self, *a, **k) -> str:
        return self.pages[min(self.at, len(self.pages) - 1)]

    async def settle(self) -> None:
        return None


class PositionsInOneCall(unittest.TestCase):
    def enter(self, pages: list[str], positions: list[str], errors: dict | None = None, calls=None, batch=True,
              deadline: float = 900.0):
        import submit
        session = FormPages(pages)
        desk = submit.PositionDesk({p: {"position": p, **ENTRY} for p in positions}, time.monotonic() + deadline)
        desk.after_positions = " ALL DONE."
        filled: list[tuple[str, dict]] = []
        errors = dict(errors or {})

        async def fill_position(sess, entry, plan, log, choices=None):
            filled.append((entry["position"], dict(choices or {})))
            problem = errors.get(entry["position"])
            if problem and not (choices or {}).get("Certification"):
                return problem
            log.append("Bore (mm) = 50")
            return None

        async def locate(page, ref):
            async def click(timeout=0):
                session.at += 1
            return SimpleNamespace(click=click)

        ctx = SimpleNamespace(deps=SimpleNamespace(session=session))
        shown = pages[0].splitlines()[0].split()[-1]  # the model names the position the page shows
        calls = calls or [dict(position=shown, fields=FIELDS, confirm_button="Apply", next_button="Save and next")]

        async def run():
            return [await desk.configure_position(ctx, **call) for call in calls]

        with patch.object(submit, "fill_position", fill_position), patch.object(submit, "locate", locate), \
                patch.object(submit, "POSITION_BATCH", batch):
            answers = asyncio.run(run())
        return desk, filled, answers, session

    def test_one_call_enters_every_position_the_form_shows_in_turn(self):
        pages = [position_page("01.02"), position_page("01.01"), position_page("01.03"), TOTALS_PAGE]
        desk, filled, answers, session = self.enter(pages, ["01.01", "01.02", "01.03"])
        self.assertEqual([p for p, _ in filled], ["01.02", "01.01", "01.03"])
        self.assertEqual(desk.entered, {"01.01", "01.02", "01.03"})
        self.assertIn("Entered and saved: 01.02", answers[0])
        self.assertIn("Position 01.03 entered", answers[0])
        self.assertIn("ALL DONE.", answers[0])
        self.assertTrue(answers[0].endswith(TOTALS_PAGE))  # the model sees the page that needs it

    def test_it_stops_at_a_position_the_bid_has_no_values_for(self):
        pages = [position_page("01.02"), position_page("01.09"), position_page("01.01"), TOTALS_PAGE]
        desk, filled, answers, _ = self.enter(pages, ["01.01", "01.02"])
        self.assertEqual([p for p, _ in filled], ["01.02"])
        self.assertTrue(answers[0].endswith(position_page("01.09")))
        self.assertNotIn("ALL DONE.", answers[0])

    def test_a_problem_stops_it_and_the_model_resolves_it_once_for_all_such_positions(self):
        pages = [position_page("01.01"), position_page("01.02"), position_page("01.03"), TOTALS_PAGE]
        problem = "'Certification': no option means 'None'"
        calls = [dict(position="01.01", fields=FIELDS, confirm_button="Apply", next_button="Save and next"),
                 dict(position="01.02", choices={"Certification": "Without certification"})]
        desk, filled, answers, _ = self.enter(pages, ["01.01", "01.02", "01.03"],
                                              errors={"01.02": problem, "01.03": problem}, calls=calls)
        self.assertIn("Entered and saved: 01.01", answers[0])
        self.assertIn("Position 01.02 not fully entered: " + problem, answers[0])
        # the second call resolves 01.02, and 01.03 (same wanted value) reuses the choice without another call
        self.assertEqual(filled[-1], ("01.03", {"Certification": "Without certification"}))
        self.assertEqual(desk.entered, {"01.01", "01.02", "01.03"})
        self.assertEqual(desk.failures, {"01.02": 1})

    def test_a_page_that_only_names_the_next_position_is_left_to_the_model(self):
        pages = [position_page("01.02"), PREVIEW_PAGE, position_page("01.03")]
        desk, filled, answers, _ = self.enter(pages, ["01.02", "01.03"])
        self.assertEqual(([p for p, _ in filled], desk.failures), (["01.02"], {}))
        self.assertTrue(answers[0].endswith(PREVIEW_PAGE))

    def test_time_running_short_ends_the_batch(self):
        import submit
        pages = [position_page("01.01"), position_page("01.02"), TOTALS_PAGE]
        desk, filled, _, _ = self.enter(pages, ["01.01", "01.02"], deadline=submit.SIGNING_RESERVE + 0.3,
                                        errors=None)
        self.assertGreaterEqual(len(filled), 1)  # never more work once the signing reserve is reached
        desk2, filled2, answers2, _ = self.enter(pages, ["01.01", "01.02"], deadline=submit.SIGNING_RESERVE - 1)
        self.assertEqual(filled2, [])
        self.assertIn("Time is short", answers2[0])

    def test_switched_off_it_is_one_position_per_call(self):
        pages = [position_page("01.01"), position_page("01.02"), TOTALS_PAGE]
        desk, filled, answers, _ = self.enter(pages, ["01.01", "01.02"], batch=False)
        self.assertEqual([p for p, _ in filled], ["01.01"])
        self.assertTrue(answers[0].endswith(position_page("01.02")))


class ListPagesShownByHand(unittest.TestCase):
    """collect_pages starts where the worker is: pages of the list it paged through by hand are read too."""

    def page(self, n: int, last: bool = False) -> str:
        pager = "" if last else '\n[e9 button "Next page"]'
        return f"# Questions and answers, page {n}\n[e3 link \"Answer {n}01\"]{pager}"

    def test_pages_before_the_collection_started_are_added(self):
        import research_tender as rt
        collected = rt.Collected(pages=[self.page(3), self.page(4, last=True)], control=("button", "Next page"))
        views = ["# Tender overview\n[e2 link \"Questions\"]", self.page(1), self.page(2), self.page(3)]
        pages = rt.pages_to_read(collected, views, "everything seen")
        self.assertEqual(pages, [self.page(3), self.page(4, last=True), self.page(1), self.page(2)])

    def test_refs_do_not_make_a_page_new_and_other_pages_stay_out(self):
        import research_tender as rt
        collected = rt.Collected(pages=[self.page(1)], control=("button", "Next page"))
        same_with_other_refs = self.page(1).replace("[e3 ", "[e31 ").replace("[e9 ", "[e97 ")
        self.assertEqual(rt.pages_to_read(collected, [same_with_other_refs, "# Start\n[e1 link \"Tenders\"]"], ""),
                         [self.page(1)])

    def test_without_a_collection_everything_seen_is_read(self):
        import research_tender as rt
        self.assertEqual(rt.pages_to_read(rt.Collected(), [self.page(1)], "everything seen"), ["everything seen"])

    def test_views_are_recorded_once_each(self):
        import research_tender as rt
        shown = iter([self.page(1), self.page(1).replace("[e3 ", "[e5 "), self.page(2)])
        session = SimpleNamespace(view="")

        async def look(offset: int = 0, **options) -> str:
            session.view = next(shown)
            return session.view
        session.look = look

        async def run():
            views = rt.record_views(session)
            for _ in range(3):
                await session.look()
            return views
        self.assertEqual(asyncio.run(run()), [self.page(1), self.page(2)])


if __name__ == "__main__":
    unittest.main()
