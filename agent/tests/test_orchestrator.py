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
