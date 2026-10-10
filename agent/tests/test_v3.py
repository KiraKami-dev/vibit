"""v3: the state loop guard, Stuck ending a worker's model run, and the specification race."""
import asyncio
import tempfile
import time
import unittest
from unittest.mock import patch

import orchestrator
from board import Board
from browser_tools import SiteSession, Stuck
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from research import SpecResult
from tracing import Trace


def _session() -> SiteSession:
    return SiteSession({"siteId": "site", "url": "https://example.test"}, None, Trace(tempfile.mkdtemp()))


class LoopGuard(unittest.TestCase):
    def test_off_by_default(self):
        s = _session()
        s.view = "[e1 link \"Dokumente\"]"
        self.assertEqual([s._loop_check("u") for _ in range(6)], [""] * 6)

    def test_warns_on_a_returning_state_and_ignores_refs(self):
        s = _session()
        s.reset_loop(guard=True)
        notes = []
        for i in range(3):
            s.view = f"[e{i + 1} link \"Dokumente\"]"  # refs renumber with every view; the state is the same
            notes.append(s._loop_check("https://example.test/docs"))
        self.assertEqual(notes[:2], ["", ""])
        self.assertIn("Loop warning", notes[2])

    def test_different_states_do_not_warn(self):
        s = _session()
        s.reset_loop(guard=True)
        for i in range(8):
            s.view = f"page {i}"
            self.assertEqual(s._loop_check("u"), "")

    def test_stop_on_loop_raises_after_the_set_number_of_warnings(self):
        s = _session()
        s.reset_loop(guard=True, stop=True)
        s.view = "same"
        with self.assertRaises(Stuck):
            for _ in range(10):
                s._loop_check("u")
        self.assertGreaterEqual(s.loop_hits, 3)


class StuckEndsAModelRun(unittest.TestCase):
    def test_stuck_from_a_tool_leaves_agent_run(self):
        agent = Agent(TestModel())

        @agent.tool_plain
        def click(ref: str) -> str:
            raise Stuck("loop")

        with self.assertRaises(Stuck):
            asyncio.run(agent.run("go"))


def _orchestrator() -> orchestrator.Orchestrator:
    o = object.__new__(orchestrator.Orchestrator)
    o.trace = Trace(tempfile.mkdtemp())
    o.board = Board(o.trace)
    o.started = time.monotonic()
    o.research_deadline = time.monotonic() + 60
    o.briefing = None
    o.requirements = {}

    async def session(site, tag=""):
        return tag
    o.session = session
    return o


def _spec(revision: str, ids: list[str], problems=()) -> SpecResult:
    r = SpecResult()
    r.current_revision = revision
    r.requirements = [type("Req", (), {"requirement_id": i, "revision": revision})() for i in ids]
    r.problems = list(problems)
    return r


class SpecRace(unittest.TestCase):
    def run_race(self, plan: dict):
        cancelled = []

        async def fake_research_spec(tag, briefing, **kwargs):
            delay, result = plan[tag]
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                cancelled.append(tag)
                raise
            return result

        o = _orchestrator()
        with patch.object(orchestrator, "research_spec", fake_research_spec), \
                patch.object(orchestrator, "SPEC_STAGGER", 0.0), \
                patch.object(orchestrator, "SPEC_VARIANTS", ["fast", "search", "explore"]):
            result = asyncio.run(o._spec_race("site", "", lambda tag: (lambda req: None),
                                              {"revision": None, "variant": None}))
        return result, cancelled, o

    def test_first_attempt_with_requirements_wins_and_the_rest_stop(self):
        result, cancelled, _ = self.run_race({
            "spec": (0.01, _spec("", [], ["no revision history found"])),  # the fast one gets lost
            "spec2": (0.05, _spec("R-2", ["A-1", "A-2"])),                 # search finds it
            "spec3": (5.0, _spec("R-2", ["A-1"])),                         # explore is still busy
        })
        self.assertEqual(result.current_revision, "R-2")
        self.assertEqual([r.requirement_id for r in result.requirements], ["A-1", "A-2"])
        self.assertEqual(cancelled, ["spec3"])

    def test_no_winner_by_the_cutoff_keeps_what_streamed(self):
        with patch.object(orchestrator, "SPEC_RACE_SECONDS", 0.2):
            result, cancelled, _ = self.run_race({
                "spec": (0.01, _spec("", [], ["no revision history found"])),
                "spec2": (30.0, _spec("R-2", ["A-1"])),  # still busy after the cutoff and its 5 s grace
                "spec3": (30.0, _spec("R-2", ["A-1"])),
            })
        self.assertEqual(result.requirements, [])
        self.assertIn("spec: no revision history found", result.problems)
        self.assertEqual(sorted(cancelled), ["spec2", "spec3"])


if __name__ == "__main__":
    unittest.main()
