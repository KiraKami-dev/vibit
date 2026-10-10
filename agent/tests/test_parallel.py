"""v3: the revision judged twice, the ERP's optional second candidate search, and the waits taken out of
the research (mail plan, rework after the verifier)."""
import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import orchestrator
import research
from board import Board, Fact, Source
from research import CandidateSeen, CurrentRevision, Erp, RevisionHistory, RevisionSeen, SearchReport
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


def report(*numbers: str, term: str = "G-1") -> SearchReport:
    return SearchReport(search_term=term, candidates=[CandidateSeen(material_number=n, link=f"/m/{n}")
                                                      for n in numbers])


REQ = SimpleNamespace(requirement_id="A-1", catalog_group="G-1", bore=50, stroke=100, pressure=160,
                      mounting="Flange", certification="None", pending=[])


class ErpSecondSearch(unittest.TestCase):
    def research(self, first, second, race: bool = True, after: float = 0.0):
        """`first` and `second` are (seconds, report) of the prescribed and the second search; with `after`, the
        second search begins as in a run: after that many seconds, or once the first returned without hits."""
        events: list[str] = []
        cancelled: list[str] = []
        erp = Erp({"siteId": "erp", "url": "https://erp.test"}, None, Trace(tempfile.mkdtemp()))

        async def search(name, plan):
            seconds, found = plan
            try:
                await asyncio.sleep(seconds)
            except asyncio.CancelledError:
                cancelled.append(name)
                raise
            events.append(f"{name} done")
            return found

        async def record(number):
            return SimpleNamespace(material_number=number)

        def material(number, link=""):
            if number not in erp.materials:
                events.append(f"inspect {number}")
                erp.materials[number] = asyncio.ensure_future(record(number))
            return erp.materials[number]

        async def choose(req, out, materials, trace):
            return (sorted(materials)[0] if materials else None), []

        async def second_search(req, start):
            if after:  # the real one: begins when told to, or after its waiting time
                return await Erp._second_search(erp, req, start)
            return await search("second", second)

        async def searches(req, tag, **options):
            return await search("second" if options.get("hint") else "first", second if options.get("hint") else first)

        erp._search = searches
        erp._second_search = second_search
        erp.material = material
        with patch.object(research, "ERP_SEARCH_RACE", race), patch.object(research, "choose_material", choose), \
                patch.object(research, "ERP_SECOND_AFTER", after or 40.0):
            out = asyncio.run(erp.requirement(REQ))
        return out, events, cancelled

    def test_prescribed_search_with_hits_stops_a_second_one_still_running(self):
        out, events, cancelled = self.research((0.01, report("M-1", "M-2")), (5.0, report("M-9")))
        self.assertEqual(out.inspected, ["M-1", "M-2"])
        self.assertEqual(cancelled, ["second"])

    def test_a_faster_second_search_gets_a_head_start_and_only_adds(self):
        out, events, cancelled = self.research((0.1, report("M-1", "M-2")), (0.01, report("M-2", "M-9")))
        self.assertEqual(out.inspected, ["M-1", "M-2", "M-9"])  # the prescribed search's hits first
        self.assertLess(events.index("inspect M-9"), events.index("first done"))
        self.assertEqual(cancelled, [])

    def test_a_prescribed_search_without_hits_waits_for_the_second(self):
        out, _, _ = self.research((0.01, report()), (0.1, report("M-9", term="G 1")))
        self.assertEqual((out.inspected, out.selected), (["M-9"], "M-9"))
        out, _, _ = self.research((0.01, None), (0.1, report("M-9", term="G 1")))
        self.assertEqual((out.inspected, out.search_term), (["M-9"], "G 1"))

    def test_a_first_search_that_works_never_starts_the_second(self):
        out, events, cancelled = self.research((0.05, report("M-1")), (0.0, report("M-9")), after=5.0)
        self.assertEqual((out.inspected, events.count("second done"), cancelled), (["M-1"], 0, []))

    def test_a_first_search_without_hits_starts_the_second_at_once(self):
        started = time.monotonic()
        out, _, _ = self.research((0.05, report()), (0.05, report("M-9")), after=5.0)
        self.assertEqual(out.inspected, ["M-9"])
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_slow_first_search_gets_the_second_beside_it(self):
        out, events, _ = self.research((0.3, report("M-1")), (0.05, report("M-9")), after=0.05)
        self.assertEqual(out.inspected, ["M-1", "M-9"])
        self.assertLess(events.index("second done"), events.index("first done"))

    def test_no_result_from_either_is_a_problem(self):
        out, _, _ = self.research((0.01, None), (0.02, None))
        self.assertEqual(out.problems, ["search worker returned no result"])

    def test_switched_off_there_is_one_search(self):
        out, events, _ = self.research((0.01, report("M-1")), (0.0, report("M-9")), race=False)
        self.assertEqual(out.inspected, ["M-1"])
        self.assertNotIn("second done", events)

    def test_a_cancelled_worker_gives_its_tab_slot_back(self):
        async def run() -> int:
            erp = Erp({"siteId": "erp", "url": "https://erp.test"}, None, Trace(tempfile.mkdtemp()))
            erp.ready = asyncio.ensure_future(asyncio.sleep(0))
            erp.active_at = time.monotonic()
            slots = erp.tabs._value
            async with erp.lock:  # another worker is signing in again
                entering = asyncio.ensure_future(erp._enter())
                await asyncio.sleep(0.01)
                entering.cancel()
                await asyncio.gather(entering, return_exceptions=True)
            return slots - erp.tabs._value

        self.assertEqual(asyncio.run(run()), 0)


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
    def verify(self, rounds: list[list[Fact]]):
        calls, reworked = [], []

        async def verify_board(board, rules, reference, scenario):
            calls.append(1)
            return rounds[len(calls) - 1]

        async def rework(facts):
            reworked.append([f.key for f in facts])

        o = bare()
        o.rework = {"mails": rework}
        with patch.object(orchestrator, "verify_board", verify_board):
            asyncio.run(o.verify())
        return len(calls), reworked

    def test_a_quote_that_does_not_show_the_value_sends_nothing_back(self):
        self.assertEqual(self.verify([[doubt("a", False), doubt("b", False)]]), (1, []))

    def test_a_doubted_record_goes_back_once_and_is_judged_again(self):
        self.assertEqual(self.verify([[doubt("a", False), doubt("b", True)], []]), (2, [["b"]]))

    def test_no_doubt_is_one_check(self):
        self.assertEqual(self.verify([[]]), (1, []))


class MailPlanHedge(unittest.TestCase):
    def plan(self, seconds: list, hedge: float = 0.05):
        calls = []

        async def plan_searches(brief, sites, findings=""):
            calls.append(len(calls) + 1)
            n = calls[-1]
            wait = seconds[n - 1]
            if isinstance(wait, Exception):
                raise wait
            await asyncio.sleep(wait)
            return [f"plan {n}"]

        o = bare()
        with patch.object(orchestrator.research_mail, "plan_searches", plan_searches), \
                patch.object(orchestrator, "PLAN_HEDGE", hedge):
            return asyncio.run(o._mail_plan({}, "")), len(calls)

    def test_a_plan_that_answers_in_time_is_one_call(self):
        self.assertEqual(self.plan([0.01]), (["plan 1"], 1))

    def test_a_stalled_plan_gets_a_second_call_and_the_first_answer_wins(self):
        self.assertEqual(self.plan([5.0, 0.01]), (["plan 2"], 2))
        self.assertEqual(self.plan([0.1, 5.0]), (["plan 1"], 2))

    def test_a_failed_call_leaves_the_other(self):
        self.assertEqual(self.plan([ValueError("bad"), 0.01], hedge=0.0), (["plan 2"], 2))
        with self.assertRaises(ValueError):
            self.plan([ValueError("bad"), ValueError("bad")], hedge=0.0)


if __name__ == "__main__":
    unittest.main()
