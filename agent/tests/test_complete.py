"""Forced to be complete, and not held up by one tab: a requirement that fails the coverage check goes back to the
ERP, a mail search that hangs on its tab is run on a free one too, and model calls keep to one budget (all on
invented data, without browsers or models)."""
import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import orchestrator  # noqa: E402
from board import Board  # noqa: E402
from evidence import RequirementResearch  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from research import ErpResearch  # noqa: E402


class Events:
    def __init__(self):
        self.events, self.last = [], 0.0

    def event(self, name, kind, quiet=False, **fields):
        self.events.append((name, kind, fields))

    def of(self, kind):
        return [fields for _, k, fields in self.events if k == kind]


def bare(seconds_left: float = 1000.0) -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    o.trace = Events()
    o.board = Board()
    o.reference, o.scenario, o.mailbox = "T-1", None, "sales@bidder.example"
    o.research_deadline = time.monotonic() + seconds_left
    o.requirements = {rid: SimpleNamespace(requirement_id=rid) for rid in ("A-1", "A-2", "A-3")}
    o.effective = lambda req: req
    o.mail_results, o.mail_sessions, o.mail_kinds = {}, [], []
    o.tender_facts, o.erp = None, None  # nothing read yet: every mail record kind may still be referred to
    for source in ("position", "requirement", "material"):  # and no source is still being read
        o.board.finish(source)
    return o


def found(rid: str, *numbers: str, no_hits: bool = False) -> RequirementResearch:
    return RequirementResearch(requirement_id=rid, inspected=list(numbers), no_hits=no_hits)


def findings(requirements, read) -> ErpResearch:
    return ErpResearch.model_construct(requirements=list(requirements), materials={n: object() for n in read})


class FakeErp:
    """An ERP stage whose requirement research returns the next prepared outcome."""

    site = {"siteId": "erp"}

    def __init__(self, first: ErpResearch, outcomes: dict[str, list]):
        self.results = {r.requirement_id: r for r in first.requirements}
        self.read = set(first.materials)
        self.tasks, self.materials, self.partial, self.calls, self.outcomes = {}, {}, {}, [], outcomes

    def start(self, req):
        self.calls.append(req.requirement_id)

        async def research():
            outcome = self.outcomes[req.requirement_id].pop(0)
            self.results[req.requirement_id] = outcome
            self.read.update(outcome.inspected if outcome.search_term != "unread" else ())
            return outcome

        self.tasks[req.requirement_id] = asyncio.ensure_future(research())
        return self.tasks[req.requirement_id]

    def so_far(self) -> ErpResearch:
        return findings(self.results.values(), self.read)


class Coverage(unittest.TestCase):
    FIRST = findings([found("A-1", "M-1"), found("A-2"), found("A-3", no_hits=True)], ["M-1"])

    def test_what_counts_as_covered(self):
        o = bare()
        o.erp = None
        self.assertEqual(o.uncovered(), [])  # no ERP findings at all: nothing this check can send back
        o.erp = self.FIRST
        self.assertEqual(o.uncovered(), ["A-2"])  # a search without a list; "no hits" said by the site is covered
        o.erp = findings([found("A-1", "M-1", "M-9"), found("A-3", no_hits=True)], ["M-1"])
        self.assertEqual(o.uncovered(), ["A-1", "A-2"])  # a listed material not read; a requirement never searched

    def test_an_uncovered_requirement_goes_back_until_it_is_covered(self):
        o = bare()
        o.erp = self.FIRST
        o._erp = FakeErp(self.FIRST, {"A-2": [found("A-2"), found("A-2", "M-2")]})
        asyncio.run(o.cover())
        self.assertEqual(o._erp.calls, ["A-2", "A-2"])  # only the one that failed, and again when it failed again
        self.assertEqual(o.uncovered(), [])
        self.assertEqual([e["requirements"] for e in o.trace.of("sent_back")], [["A-2"], ["A-2"]])
        self.assertEqual(o.trace.of("done"), [{"covered": ["A-2"], "still_missing": []}])
        self.assertIn(("material", "A-2"), o.board.facts)

    def test_it_stops_after_its_rounds_and_says_what_is_missing(self):
        o = bare()
        o.erp = self.FIRST
        o._erp = FakeErp(self.FIRST, {"A-2": [found("A-2") for _ in range(9)]})
        asyncio.run(o.cover())
        self.assertEqual(len(o._erp.calls), orchestrator.COVERAGE_ROUNDS)
        self.assertEqual(o.trace.of("done"), [{"covered": [], "still_missing": ["A-2"]}])

    def test_a_material_called_inactive_on_a_guess_is_read_once_more(self):
        o = bare()
        guess = SimpleNamespace(active=False, problems=["status meaning not read"])
        blocked = SimpleNamespace(active=False, problems=[])  # its status was explained: not a guess
        first = ErpResearch.model_construct(
            requirements=[found("A-1", "M-1"), found("A-2", "M-2"), found("A-3", no_hits=True)],
            materials={"M-1": guess, "M-2": blocked})
        o.erp = first
        o._erp = FakeErp(first, {"A-1": [found("A-1", "M-1")]})
        read = SimpleNamespace(done=lambda: True, cancelled=lambda: False, exception=lambda: None,
                               result=lambda: "a record")
        o._erp.materials = {"M-1": read, "M-2": read}
        self.assertEqual(o.uncovered(), ["A-1"])
        asyncio.run(o.cover())
        self.assertEqual(o._erp.calls, ["A-1"])  # sent back once: a second guess is not read a third time
        self.assertEqual(sorted(o._erp.materials), ["M-2"])  # the guessed reading was dropped, to be read afresh

    def test_the_rounds_are_counted_over_both_calls(self):
        # the check runs when the ERP research ends and once more after all research
        o = bare()
        o.erp = self.FIRST
        o._erp = FakeErp(self.FIRST, {"A-2": [found("A-2") for _ in range(9)]})
        asyncio.run(o.cover())
        asyncio.run(o.cover())
        self.assertEqual(len(o._erp.calls), orchestrator.COVERAGE_ROUNDS)

    def test_nothing_is_spent_when_all_is_covered_or_time_is_short(self):
        covered = findings([found("A-1", "M-1"), found("A-2", "M-2"), found("A-3", no_hits=True)], ["M-1", "M-2"])
        for o, first in ((bare(), covered), (bare(seconds_left=30), self.FIRST)):
            o.erp = first
            o._erp = FakeErp(first, {})
            asyncio.run(o.cover())
            self.assertEqual((o._erp.calls, o.trace.events), ([], []))


class MailStraggler(unittest.TestCase):
    def search(self, hang_first_search_of: str | None):
        o = bare()
        kinds = [SimpleNamespace(name=name) for name in ("approval", "packaging", "fx", "bonus")]
        seen: list[tuple[str, str]] = []
        cancelled: list[str] = []

        async def research_kind(session, kind, reference, deadline=None, seconds=None):
            first = kind.name not in [k for k, _ in seen]
            seen.append((kind.name, session.name))
            try:
                await asyncio.sleep(60 if first and kind.name == hang_first_search_of else 0.05)
            except asyncio.CancelledError:
                cancelled.append(kind.name)
                raise
            return SimpleNamespace(kind=kind, records=[f"{kind.name} record"], kept=["view"], seconds=0.05,
                                   stopped=False)

        mail = SimpleNamespace(research_kind=research_kind, KIND_SECONDS=150.0,
                               resolve_mail=lambda *a: SimpleNamespace(problems=[], sources=[], approval=None))

        async def run():
            async def opened(i):
                return SimpleNamespace(name=f"mail{i}", site={"siteId": f"mail:mail{i}"})
            starts = [asyncio.ensure_future(opened(i)) for i in range(1, 5)]
            started = time.monotonic()
            with patch.object(orchestrator, "research_mail", mail):
                await o._research_mail(kinds, deadline=time.monotonic() + 600, starts=starts)
            return time.monotonic() - started
        seconds = asyncio.run(run())
        return o, seen, cancelled, seconds

    def test_a_free_tab_takes_over_a_search_that_hangs(self):
        o, seen, cancelled, seconds = self.search("packaging")
        self.assertLess(seconds, 10)  # not the 60 s the hanging tab would have taken
        self.assertEqual(set(o.mail_results), {"approval", "packaging", "fx", "bonus"})
        self.assertEqual(o.mail_results["packaging"].records, ["packaging record"])
        self.assertEqual(cancelled, ["packaging"])  # the search on the hanging tab was ended
        self.assertEqual(len({tab for kind, tab in seen if kind == "packaging"}), 2)
        self.assertEqual([e["record_kind"] for e in o.trace.of("mail_help")], ["packaging"])

    def test_no_search_is_doubled_when_none_hangs(self):
        o, seen, cancelled, _ = self.search(None)
        self.assertEqual(sorted(kind for kind, _ in seen), ["approval", "bonus", "fx", "packaging"])
        self.assertEqual((cancelled, o.trace.of("mail_help")), ([], []))


class UnstatedAttributes(unittest.TestCase):
    """A value the specification leaves out is not compared; it does not cost the requirement."""

    def seen(self, **update):
        import research
        values = dict(requirement_id="R-1", bore="50", stroke="200", pressure="160 bar", mounting="Flange",
                      certification="none required", catalog_group="G-7", stroke_is_usable_travel=False)
        return research.RequirementSeen(**{**values, **update})

    def test_a_requirement_without_pressure_is_kept_and_matched_on_the_rest(self):
        import research
        from decimal import Decimal as D
        from evidence import ParseError, effective_requirement, mismatches
        req = research._parse_requirement(self.seen(pressure=""), "Rev B")
        self.assertEqual((req.bore, req.pressure), (D("50"), None))
        want = effective_requirement(req)
        self.assertNotIn("pressure", want.attributes())
        material = {"bore": D("50"), "stroke": D("200"), "pressure": D("250"), "mounting": "flange",
                    "certification": want.attributes()["certification"]}
        self.assertEqual(mismatches(want, material), [])  # whatever the material's pressure is
        self.assertEqual(mismatches(want, {**material, "bore": D("63")}), ["bore 63 instead of 50"])
        self.assertNotIn("pressure", research._want(want))
        # stated attributes are compared as before, and a value that is there but unreadable is still an error
        full = effective_requirement(research._parse_requirement(self.seen(), "Rev B"))
        self.assertEqual(mismatches(full, material), ["pressure 250 instead of 160"])
        with self.assertRaises(ParseError):
            research._parse_requirement(self.seen(pressure="see drawing"), "Rev B")

    def test_no_catalogue_group_means_no_search_and_nothing_to_send_back(self):
        import research
        req = research._parse_requirement(self.seen(catalog_group=""), "Rev B")
        erp = research.Erp({"siteId": "erp", "url": "https://erp.test/"}, None, Events(), time.monotonic() + 900)
        from evidence import effective_requirement
        out = asyncio.run(erp.requirement(effective_requirement(req)))
        self.assertEqual((out.inspected, out.no_hits), ([], False))
        self.assertIn("no catalogue group", out.problems[0])
        o = bare()
        o.requirements = {"R-1": req}
        o.erp = findings([out], [])
        self.assertEqual(o.uncovered(), [])


class StatusInWords(unittest.TestCase):
    def test_a_status_written_as_a_word_needs_no_legend(self):
        import research
        for status in ("gesperrt", "aktiv", "blocked", "Freigegeben für Verkauf"):
            self.assertTrue(research.STATUS_WORD.fullmatch(status), status)
        for code in ("Z3", "03", "FRG", "frei", "A1-gesperrt", ""):
            self.assertFalse(research.STATUS_WORD.fullmatch(code), code)  # a code keeps needing its meaning


class LastResort(unittest.TestCase):
    def test_the_form_instructions_say_what_to_do_without_an_omit_option(self):
        import submit
        text = submit._positions({"decision": "bid", "discount_percent": 3.5}, submit.PositionDesk({}, 0), False)
        self.assertIn("leave every position out", text)
        self.assertIn(submit.NO_OMIT, text)
        self.assertNotIn(submit.NO_OMIT, submit._positions({"decision": "no_bid"}, submit.PositionDesk({}, 0), False))


class MailSurvey(unittest.TestCase):
    """The mailbox is read once for all kinds; only what that did not find is searched on its own."""

    def run_mail(self, survey_finds: dict, fails: bool = False, finds_nothing: bool = False):
        o = bare()
        kinds = [SimpleNamespace(name=name) for name in ("approval", "packaging", "fx")]
        searched: list[str] = []
        self.budgets: dict[str, list] = {}

        async def survey(session, kinds, reference, deadline=None, on_assigned=None):
            if fails:
                raise RuntimeError("no list")
            on_assigned({name for name, records in survey_finds.items() if records is not None}, True)
            await asyncio.sleep(0.05)  # reading the messages
            return {kind.name: SimpleNamespace(kind=kind, records=list(survey_finds[kind.name]), kept=["view"],
                                               seconds=0.1, stopped=False)
                    for kind in kinds if survey_finds.get(kind.name) is not None}

        async def research_kind(session, kind, reference, deadline=None, seconds=None):
            searched.append(kind.name)
            self.budgets.setdefault(kind.name, []).append(seconds)
            await asyncio.sleep(0.02)
            if finds_nothing:  # ran to the end of its time without a message
                return SimpleNamespace(kind=kind, records=[], kept=[], seconds=seconds, stopped=True)
            return SimpleNamespace(kind=kind, records=[f"{kind.name} by search"], kept=["view"], seconds=0.02,
                                   stopped=False)

        mail = SimpleNamespace(survey=survey, research_kind=research_kind, KIND_SECONDS=150.0,
                               resolve_mail=lambda *a: SimpleNamespace(problems=[], sources=[], approval=None))

        async def run():
            async def opened(i):
                return SimpleNamespace(name=f"mail{i}", site={"siteId": f"mail:mail{i}"})
            starts = [asyncio.ensure_future(opened(i)) for i in range(1, 3)]
            with patch.object(orchestrator, "research_mail", mail):
                await o._research_mail(kinds, deadline=time.monotonic() + 600, starts=starts, survey=True)
        asyncio.run(run())
        return o, searched

    def test_kinds_the_survey_read_are_not_searched_again(self):
        # approval read; packaging had rows but no record came of them; fx had no row at all
        o, searched = self.run_mail({"approval": ["approval record"], "packaging": [], "fx": None})
        self.assertEqual(o.mail_results["approval"].records, ["approval record"])
        self.assertEqual(sorted(searched), ["fx", "packaging"])
        self.assertEqual(o.mail_results["fx"].records, ["fx by search"])

    def test_a_kind_the_whole_mailbox_lists_nothing_for_gets_one_short_search(self):
        # packaging had rows, fx none in any folder; neither search finds a message
        o, searched = self.run_mail({"approval": ["approval record"], "packaging": [], "fx": None},
                                    finds_nothing=True)
        self.assertEqual(searched.count("fx"), 1)  # no second try for a record that is not there
        self.assertLessEqual(self.budgets["fx"][0], orchestrator.ABSENT_SECONDS)
        self.assertEqual(searched.count("packaging"), 2)  # rows were there: its search is tried once more
        self.assertGreater(self.budgets["packaging"][0], orchestrator.ABSENT_SECONDS)

    def test_without_a_survey_result_every_kind_is_searched_as_before(self):
        o, searched = self.run_mail({}, fails=True)
        self.assertEqual(sorted(searched), ["approval", "fx", "packaging"])
        self.assertTrue(any("mailbox survey" in p for p in o.board.problems))


class CallBudget(unittest.TestCase):
    def out_at_once(self, limit: str) -> int:
        active = peak = 0

        async def call():
            nonlocal active, peak
            async with llm._slot():
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1

        async def run():
            await asyncio.gather(*(call() for _ in range(6)))
        with patch.dict(os.environ, {"LLM_CONCURRENCY": limit}):
            asyncio.run(run())
        return peak

    def test_calls_out_at_once_keep_to_the_budget(self):
        self.assertEqual(self.out_at_once("2"), 2)
        self.assertEqual(self.out_at_once(""), 6)  # the default is far above six
        self.assertEqual(self.out_at_once("nonsense"), 6)


if __name__ == "__main__":
    unittest.main()
