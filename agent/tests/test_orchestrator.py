"""Model-free parts of the v2 orchestrator and the findings board, on invented facts."""
import asyncio
import sys
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calc  # noqa: E402
from board import Board, Source  # noqa: E402
from evidence import Requirement, SpecResult  # noqa: E402
from orchestrator import Orchestrator, answer_from, rules_text, safe_calculate, scenario_time  # noqa: E402

MATERIAL = calc.Material(material_number="M-1", free_stock=10, stock_lead_time_weeks=2, production_lead_time_weeks=6)
CONDITION = calc.PriceCondition(valid_from=date(2026, 1, 1), scales=[calc.PriceScale(min_quantity=0, price=D("10"))])


def position(number: str, **update) -> calc.PositionInput:
    return calc.PositionInput(position=number, quantity=5, material=MATERIAL, conditions=[CONDITION]).model_copy(
        update=update)


class SafeCalculate(unittest.TestCase):
    def bid(self, *positions) -> calc.BidInput:
        return calc.BidInput(tender_reference="T-1", scenario_date=date(2026, 6, 1), positions=list(positions))

    def test_complete_bid_has_no_blanks(self):
        result, blank, problems = safe_calculate(self.bid(position("1")))
        self.assertEqual([p.position for p in result.positions], ["1"])
        self.assertEqual(blank, {})
        self.assertEqual(problems, [])

    def test_missing_carton_size_blanks_packaging_and_unit_price(self):
        tariff = calc.PackagingTariff(price_per_carton=D("3"))
        result, blank, _ = safe_calculate(self.bid(position("1", packaging=tariff)))
        self.assertEqual(blank, {"1": {"packaging_per_unit", "unit_price"}})
        answer = answer_from(result, blank)
        self.assertNotIn("packaging_per_unit", answer["positions"][0])
        self.assertNotIn("unit_price", answer["positions"][0])
        self.assertEqual(answer["positions"][0]["base_unit_price"], 10.0)

    def test_missing_metal_index_blanks_surcharge(self):
        heavy = MATERIAL.model_copy(update={"surcharge_mass": D("1")})
        _, blank, _ = safe_calculate(self.bid(position("1", material=heavy)))
        self.assertEqual(blank, {"1": {"surcharge_per_unit", "unit_price"}})

    def test_unpriceable_position_is_omitted(self):
        result, _, problems = safe_calculate(self.bid(position("1"), position("2", conditions=[])))
        self.assertEqual([p.position for p in result.positions], ["1"])
        self.assertTrue(any("position 2 omitted" in p for p in problems))


class Helpers(unittest.TestCase):
    def test_scenario_time(self):
        self.assertEqual(scenario_time("2026-06-01T10:00:00Z"), datetime(2026, 6, 1, 10, tzinfo=UTC))
        self.assertEqual(scenario_time("2026-06-01"), datetime(2026, 6, 1, 23, 59, 59, tzinfo=UTC))

    def test_rules_text(self):
        self.assertEqual(rules_text("intro\n## Rules\n1. x"), "## Rules\n1. x")

    def test_unresolved_keys_blank_their_fields(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator.blank_hints = {}
        p = SimpleNamespace(position="1", packaging_key="K-1", setup_pool_key="P-1", bonus_key=None)
        orchestrator._unknown_clauses(p, SimpleNamespace(packaging=None), None, None)
        self.assertEqual(orchestrator.blank_hints,
                         {"1": {"packaging_per_unit", "surcharge_per_unit", "unit_price"}})
        orchestrator.blank_hints = {}
        pool = SimpleNamespace(members=["1"])
        tariff = calc.PackagingTariff(price_per_carton=D("3"))
        orchestrator._unknown_clauses(p, SimpleNamespace(packaging=tariff), pool, None)
        self.assertEqual(orchestrator.blank_hints, {})


def bare_orchestrator(approval=None) -> Orchestrator:
    """An orchestrator without browsers or models, for its bookkeeping."""
    o = Orchestrator.__new__(Orchestrator)
    o.board, o.requirements, o.spec_revision = Board(), {}, None
    o.reference, o.scenario_stamp = "T-1", date(2026, 6, 1)
    o.mail_facts = SimpleNamespace(approval=approval)
    o.tender_facts, o.erp = None, None
    return o


def requirement(rid: str, revision: str, bore: str = "50") -> Requirement:
    return Requirement(requirement_id=rid, revision=revision, bore=D(bore), stroke=D("100"), pressure=D("160"),
                       mounting="Flange", certification="None", catalog_group="G-1")


class FakeErp:
    def __init__(self):
        self.started, self.forgotten = [], []

    def start(self, req):
        self.started.append((req.requirement_id, req.bore))

    def forget(self, rid):
        self.forgotten.append(rid)


class PartialAnswers(unittest.TestCase):
    def test_fallback_keeps_reference_and_unconditional_discount(self):
        answer = bare_orchestrator(calc.Approval(percent=D("3"))).fallback_answer()
        self.assertEqual((answer["tender_reference"], answer["decision"], answer["discount_percent"],
                          answer["positions"], answer["total_net"]), ("T-1", "bid", 3.0, [], 0.0))

    def test_fallback_conditional_discount_without_positions_is_zero(self):
        approval = calc.Approval(percent=D("3"), min_order_value=D("1000"))
        self.assertEqual(bare_orchestrator(approval).fallback_answer()["discount_percent"], 0.0)

    def test_fallback_without_approval(self):
        self.assertEqual(bare_orchestrator().fallback_answer()["discount_percent"], 0.0)


class SpecAttempts(unittest.TestCase):
    def setUp(self):
        self.o = bare_orchestrator()
        self.o.effective = lambda req: req
        self.erp = FakeErp()

    def test_requirements_stream_once_and_from_one_revision(self):
        self.o._requirement_found(requirement("A-1", "R-2"), "s", "", "", self.erp)
        self.o._requirement_found(requirement("A-1", "R-2", "63"), "s", "", "", self.erp)  # the other attempt
        self.o._requirement_found(requirement("A-2", "R-1"), "s", "", "", self.erp)  # another revision
        self.assertEqual(self.erp.started, [("A-1", D("50"))])
        self.assertEqual(set(self.o.requirements), {"A-1"})

    def test_winner_of_another_revision_replaces_the_streamed_ones(self):
        self.o._requirement_found(requirement("A-1", "R-1"), "s", "", "", self.erp)
        spec = SpecResult(current_revision="R-2", requirements=[requirement("A-1", "R-2", "63"),
                                                                 requirement("A-2", "R-2")])
        self.o._adopt_revision(spec, "s", self.erp)
        self.assertEqual(self.erp.forgotten, ["A-1"])
        self.assertEqual({k: r.revision for k, r in self.o.requirements.items()}, {"A-1": "R-2", "A-2": "R-2"})

    def test_partial_winner_keeps_streamed_requirements(self):
        self.o._requirement_found(requirement("A-1", "R-2"), "s", "", "", self.erp)
        self.o._adopt_revision(SpecResult(current_revision="R-2", requirements=[requirement("A-3", "R-2")]),
                               "s", self.erp)
        self.assertEqual(set(self.o.requirements), {"A-1", "A-3"})


class DeadlineKeepsWork(unittest.TestCase):
    def test_step_budget_grows_with_time_left(self):
        import time

        from worker import steps_for
        self.assertEqual(steps_for(12, None), 12)
        self.assertEqual(steps_for(12, time.monotonic() + 1000), 24)
        self.assertEqual(steps_for(12, time.monotonic() + 10), 12)

    def test_erp_so_far_keeps_finished_and_marks_unfinished(self):
        from evidence import RequirementResearch
        from research import Erp

        async def scenario():
            erp = Erp.__new__(Erp)
            done = RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1")

            async def finished():
                return done

            erp.tasks = {"A-1": asyncio.create_task(finished()), "A-2": asyncio.create_task(asyncio.sleep(10))}
            erp.partial = {"A-2": RequirementResearch(requirement_id="A-2", inspected=["M-2", "M-3"])}
            erp.materials = {}
            await asyncio.sleep(0)
            out = erp.so_far()
            erp.tasks["A-2"].cancel()
            return out

        out = asyncio.run(scenario())
        by_id = {r.requirement_id: r for r in out.requirements}
        self.assertEqual(by_id["A-1"].selected, "M-1")
        self.assertIsNone(by_id["A-2"].selected)
        self.assertTrue(by_id["A-2"].problems)  # never read as "no material", so never a no_bid


class BoardEvents(unittest.TestCase):
    def test_wait_returns_once_the_producer_finishes(self):
        async def scenario():
            board = Board()

            async def producer():
                await asyncio.sleep(0.01)
                board.post("position", "1", {"quantity": 5}, Source(site="s"), "tender")
                board.finish("position")

            asyncio.create_task(producer())
            return await board.wait("position", timeout=2)

        facts = asyncio.run(scenario())
        self.assertEqual(facts["1"].value, {"quantity": 5})

    def test_wait_times_out_with_what_exists(self):
        async def scenario():
            board = Board()
            board.post("mail", "a", 1, Source(site="s"))
            return await board.wait("mail", timeout=0.01)

        self.assertEqual(set(asyncio.run(scenario())), {"a"})


if __name__ == "__main__":
    unittest.main()
