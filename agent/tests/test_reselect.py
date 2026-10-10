"""reselect(): earlier valid choices stand without a model call, and a slow choice never discards the others."""
import asyncio
import sys
import unittest
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import research  # noqa: E402
from evidence import EffectiveRequirement, MaterialRecord, RequirementResearch  # noqa: E402


def req(rid: str, stroke: str | None = "100") -> EffectiveRequirement:
    return EffectiveRequirement(requirement_id=rid, catalog_group="GR-1", bore=D("50"),
                                stroke=D(stroke) if stroke else None, pressure=D("200"), mounting="Flansch",
                                certification="keine", pending=[] if stroke else ["reserve"])


def record(number: str, stroke: str, active: bool = True) -> MaterialRecord:
    return MaterialRecord(material_number=number, active=active, status="01", bore=D("50"), stroke=D(stroke),
                          pressure=D("200"), mounting="Flansch", certification="keine")


class ReselectTest(unittest.TestCase):
    def setUp(self):
        self.original = research.choose_material
        self.asked: list[str] = []

    def tearDown(self):
        research.choose_material = self.original

    def erp(self) -> research.ErpResearch:
        return research.ErpResearch(
            requirements=[RequirementResearch(requirement_id="A-1", inspected=["M-1", "M-2"], selected="M-1"),
                          RequirementResearch(requirement_id="A-2", inspected=["M-3"], selected=None,
                                              problems=["A-2: stroke pending (reserve)"]),
                          RequirementResearch(requirement_id="A-3", inspected=["M-4"], selected=None)],
            materials={"M-1": record("M-1", "100"), "M-2": record("M-2", "120"), "M-3": record("M-3", "130"),
                       "M-4": record("M-4", "100")})

    def test_valid_choice_stands_and_slow_choice_is_bounded(self):
        async def fake(req, research_, materials, trace=None):
            self.asked.append(req.requirement_id)
            if req.requirement_id == "A-3":
                await asyncio.sleep(5)  # a hung model call
            return "M-3", []

        research.choose_material = fake
        erp = self.erp()
        chosen = asyncio.run(research.reselect([req("A-1"), req("A-2", "130"), req("A-3")], erp, budget=0.3))
        self.assertEqual(chosen["A-1"], "M-1")  # earlier choice still fits: no model call
        self.assertNotIn("A-1", self.asked)
        self.assertEqual(chosen["A-2"], "M-3")  # chosen in time, kept despite A-3's timeout
        self.assertEqual(erp.requirements[1].problems, [])  # the stale "stroke pending" is gone
        self.assertIsNone(chosen["A-3"])  # timed out: no material, and a problem so it is not read as no_bid
        self.assertTrue(any("not made in time" in p for p in erp.requirements[2].problems))

    def test_earlier_choice_that_no_longer_fits_is_asked_again(self):
        async def fake(req, research_, materials, trace=None):
            self.asked.append(req.requirement_id)
            return "M-2", []

        research.choose_material = fake
        chosen = asyncio.run(research.reselect([req("A-1", "120")], self.erp()))
        self.assertEqual(self.asked, ["A-1"])
        self.assertEqual(chosen["A-1"], "M-2")


if __name__ == "__main__":
    unittest.main()
