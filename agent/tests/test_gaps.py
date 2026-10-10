"""The gap-filling second pass: gap detection, merging and the timing guard, on invented facts and fake workers."""
import asyncio
import sys
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gaps  # noqa: E402
from board import Board, Source  # noqa: E402
from evidence import EffectiveRequirement, MaterialRecord, Requirement, RequirementResearch  # noqa: E402
from gaps import GAP_MIN_ROUND, GAP_TAIL, Calculation, Gap, GapFiller, better, detect_gaps  # noqa: E402
from research import ErpResearch  # noqa: E402


class Events:
    def __init__(self):
        self.events = []

    def event(self, worker, kind, quiet=False, **data):
        self.events.append((kind, data))

    def kinds(self, kind):
        return [d for k, d in self.events if k == kind]


def requirement(rid: str, reserve_key: str | None = None) -> Requirement:
    return Requirement(requirement_id=rid, revision="R-1", bore=D("50"), stroke=D("100"), pressure=D("160"),
                       mounting="Flange", certification="None", catalog_group="G-1",
                       stroke_is_usable=reserve_key is not None, reserve_key=reserve_key)


def complete_record(number: str) -> MaterialRecord:
    import calc
    from datetime import date
    return MaterialRecord(material_number=number, active=True, status="active", free_stock=5,
                          stock_lead_time_weeks=1, production_lead_time_weeks=4, pieces_per_carton=2,
                          allocation_mass_kg=D("1"), surcharge_mass_kg=D("1"),
                          conditions=[calc.PriceCondition(valid_from=date(2026, 1, 1),
                                                          scales=[calc.PriceScale(min_quantity=0, price=D("10"))])])


def position(number: str, req: str, **keys):
    base = {"condition_group": None, "packaging_key": None, "commitment_key": None, "setup_pool_key": None,
            "bonus_key": None}
    return SimpleNamespace(position=number, requirement=req, **{**base, **keys})


def mail_facts(**tables):
    base = {"approval": object(), "discount_scopes": {}, "packaging": {}, "commitments": {}, "setup_pools": {},
            "project_bonuses": {}, "end_reserves": {}}
    return SimpleNamespace(**{**base, **tables})


def state(positions, requirements, research=(), materials=None, chosen=None, mail=None, reserves=None):
    """An orchestrator stand-in with just the state detect_gaps reads."""
    o = SimpleNamespace(plan=SimpleNamespace(needs=[SimpleNamespace(kind=k, site=f"site-{k}") for k in
                                                    ("tender", "specification", "materials", "mails")]),
                        tender_facts=SimpleNamespace(positions=positions),
                        requirements={r.requirement_id: r for r in requirements},
                        erp=ErpResearch(requirements=list(research), materials=materials or {}),
                        chosen=chosen or {}, mail_facts=mail if mail is not None else mail_facts(),
                        board=Board(), trace=Events(), mail_kinds=[], mail_results={})

    def effective(req):
        pending = [f"per-end reserve for key {req.reserve_key}"] if (
            req.stroke_is_usable and req.reserve_key not in (reserves or {})) else []
        return EffectiveRequirement(requirement_id=req.requirement_id, catalog_group=req.catalog_group,
                                    bore=req.bore, stroke=None if pending else req.stroke, pressure=req.pressure,
                                    mounting=req.mounting, certification=req.certification, pending=pending)

    o.effective = effective
    return o


def idents(found):
    return {g.ident: g.positions for g in found}


class DetectGaps(unittest.TestCase):
    def test_complete_answer_has_no_gaps(self):
        o = state([position("1", "A-1")], [requirement("A-1")],
                  [RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1")],
                  {"M-1": complete_record("M-1")})
        self.assertEqual(detect_gaps(o), [])

    def test_each_reason_for_an_omitted_position(self):
        o = state([position("1", "A-1"), position("2", "A-2"), position("3", "A-3"), position("4", "A-4"),
                   position("5", "A-5"), position("6", "A-6")],
                  [requirement("A-2"), requirement("A-3"), requirement("A-4", "K-9"), requirement("A-5"),
                   requirement("A-6")],
                  [RequirementResearch(requirement_id="A-2", problems=["search worker returned no result"]),
                   RequirementResearch(requirement_id="A-3", inspected=["M-3"], problems=["no valid choice"]),
                   RequirementResearch(requirement_id="A-4", inspected=["M-4"], problems=["stroke pending"]),
                   RequirementResearch(requirement_id="A-5", inspected=["M-5"]),  # complete search, no match
                   RequirementResearch(requirement_id="A-6", inspected=["M-6"], selected="M-6")])
        self.assertEqual(idents(detect_gaps(o)), {
            "requirement/A-1": ("1",), "erp/A-2": ("2",), "erp/A-3": ("3",), "mail/end_reserves": ("4",),
            "material/M-6": ("6",)})

    def test_unresolved_keys_and_calc_problems_name_their_mail_kind(self):
        o = state([position("1", "A-1", packaging_key="P-1", condition_group="K-1"),
                   position("2", "A-1", packaging_key="P-1", bonus_key="B-1")],
                  [requirement("A-1")],
                  [RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1")],
                  {"M-1": complete_record("M-1")}, mail=mail_facts(approval=None, discount_scopes={"K-1": "x"}))
        found = idents(detect_gaps(o, ["position 2 omitted: no FX rate for USD",
                                       "position 1: no metal index rate valid on 2026-06-01; field left blank"]))
        self.assertEqual(found, {"mail/approval": (), "mail/packaging": ("1", "2"), "mail/project_bonus": ("2",),
                                 "mail/fx": ("2",), "mail/metal_indices": ("1",)})

    def test_irrelevant_missing_material_facts_are_no_gap(self):
        record = complete_record("M-1").model_copy(update={"pieces_per_carton": None, "allocation_mass_kg": None})
        o = state([position("1", "A-1")], [requirement("A-1")],
                  [RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1")], {"M-1": record})
        self.assertEqual(detect_gaps(o), [])
        o.tender_facts.positions = [position("1", "A-1", packaging_key="P-1")]
        o.mail_facts.packaging = {"P-1": object()}
        self.assertEqual(idents(detect_gaps(o)), {"material/M-1": ("1",)})

    def test_no_positions_and_doubts(self):
        o = state([], [])
        o.board.post("mail", "fx/USD", "1.1", Source(site="s"), "mails").verdict = "future-dated"
        self.assertEqual(set(idents(detect_gaps(o))), {"tender/positions", "doubt/mail/fx/USD"})


class Choosing(unittest.TestCase):
    def test_better(self):
        full = Calculation(SimpleNamespace(decision="bid", positions=[1, 2]), {}, [])
        partial = Calculation(SimpleNamespace(decision="bid", positions=[1]), {}, [])
        blanked = Calculation(SimpleNamespace(decision="bid", positions=[1, 2]), {"1": {"unit_price"}}, [])
        no_bid = Calculation(SimpleNamespace(decision="no_bid", positions=[]), {}, [])
        self.assertTrue(better(full, partial))
        self.assertFalse(better(partial, full))
        self.assertFalse(better(blanked, full))
        self.assertTrue(better(no_bid, full))  # new evidence decides
        self.assertFalse(better(Calculation(None, {}, []), full))

    def test_merge_never_loses_research(self):
        o = state([], [requirement("A-1")],
                  [RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1")])
        o._erp = SimpleNamespace(materials={})
        filler = GapFiller(o)
        filler.merge(RequirementResearch(requirement_id="A-1", problems=["search worker returned no result"]))
        self.assertEqual(o.erp.requirements[0].selected, "M-1")
        filler.merge(RequirementResearch(requirement_id="A-1", inspected=["M-2"], selected="M-2"))
        self.assertEqual([r.selected for r in o.erp.requirements], ["M-2"])
        self.assertEqual(o.chosen["A-1"], "M-2")
        filler._keep_material("M-2", complete_record("M-2"))
        filler._keep_material("M-2", complete_record("M-2").model_copy(update={"free_stock": None}))
        self.assertEqual(o.erp.materials["M-2"].free_stock, 5)

    def test_a_mail_kind_that_read_records_gets_one_retry(self):
        o = state([], [])
        o.mail_results = {"packaging": SimpleNamespace(records=[object()], stopped=False),
                          "fx": SimpleNamespace(records=[], stopped=True)}
        filler = GapFiller(o)
        self.assertEqual(filler.limit(Gap("mail", "packaging", "")), 1)
        self.assertEqual(filler.limit(Gap("mail", "fx", "")), gaps.GAP_TRIES)


def runnable(seconds_left: float):
    """A stand-in with an ERP gap (A-1 never researched) and a research deadline `seconds_left` from now."""
    o = state([position("1", "A-1")], [requirement("A-1")])
    o.research_deadline = time.monotonic() + seconds_left
    o._erp = SimpleNamespace(materials={})
    return o


def calc_for(o):
    def calculate():
        n = 1 if any(r.selected for r in o.erp.requirements) else 0
        return Calculation(SimpleNamespace(decision="bid", positions=[1] * n), {}, [])
    return calculate


class Scheduling(unittest.TestCase):
    def test_no_round_without_time(self):
        o = runnable(GAP_TAIL + GAP_MIN_ROUND - 5)
        filler = GapFiller(o)
        called = []

        async def erp_requirement(rid, reason):
            called.append(rid)

        filler.erp_requirement = erp_requirement
        first = calc_for(o)()
        best = asyncio.run(filler.run(first, calc_for(o)))
        self.assertIs(best, first)
        self.assertEqual(called, [])
        self.assertEqual(o.trace.kinds("gap")[0]["gap"], "erp/A-1")
        self.assertEqual(o.trace.kinds("gap_no_time")[0]["open"], ["erp/A-1"])

    def test_round_fills_the_gap_and_keeps_the_better_answer(self):
        o = runnable(GAP_TAIL + GAP_MIN_ROUND + 60)
        filler = GapFiller(o)

        async def erp_requirement(rid, reason):
            filler.merge(RequirementResearch(requirement_id=rid, inspected=["M-1"], selected="M-1"))
            filler._keep_material("M-1", complete_record("M-1"))

        async def reselect(rids):
            self.assertEqual(rids, {"A-1"})

        filler.erp_requirement, filler.reselect = erp_requirement, reselect
        best = asyncio.run(filler.run(calc_for(o)(), calc_for(o)))
        self.assertEqual(len(best.result.positions), 1)
        self.assertEqual(filler.round, 1)
        self.assertEqual(o.trace.kinds("gap_filled"), [{"gap": "erp/A-1", "round": 1}])
        self.assertEqual(o.trace.kinds("gap_open"), [])

    def test_retries_stop_after_their_tries(self):
        o = runnable(GAP_TAIL + GAP_MIN_ROUND + 60)
        filler = GapFiller(o)
        calls = []

        async def erp_requirement(rid, reason):
            calls.append(rid)

        async def reselect(rids):
            pass

        filler.erp_requirement, filler.reselect = erp_requirement, reselect
        asyncio.run(filler.run(calc_for(o)(), calc_for(o)))
        self.assertEqual(calls, ["A-1"] * gaps.GAP_TRIES)
        self.assertEqual(o.trace.kinds("gap_open")[0]["gaps"], ["erp/A-1"])

    def test_a_failing_pass_keeps_the_first_answer(self):
        o = runnable(GAP_TAIL + GAP_MIN_ROUND + 60)
        filler = GapFiller(o)

        async def broken(_):
            raise RuntimeError("bug")

        filler.fill = broken
        first = calc_for(o)()
        self.assertIs(asyncio.run(filler.run(first, calc_for(o))), first)
        self.assertTrue(any("second pass failed" in p for p in o.board.problems))

    def test_a_slow_retry_is_cut_at_the_deadline(self):
        o = runnable(GAP_TAIL + 0.3)
        filler = GapFiller(o)

        async def erp_requirement(rid, reason):
            await asyncio.sleep(30)

        filler.erp_requirement = erp_requirement
        started = time.monotonic()
        asyncio.run(filler.fill([Gap("erp", "A-1", "ERP not researched", ("1",))]))
        self.assertLess(time.monotonic() - started, 2)
        self.assertLessEqual(time.monotonic(), o.research_deadline - GAP_TAIL + 1.5)
        self.assertTrue(any("stopped at the deadline" in p for p in o.board.problems))

    def test_retries_run_side_by_side(self):
        o = runnable(GAP_TAIL + 5)
        filler = GapFiller(o)
        running, peak = [0], [0]

        async def busy(*_):
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            await asyncio.sleep(0.05)
            running[0] -= 1

        filler.erp_requirement = busy
        filler.material = busy
        filler.mails = busy
        asyncio.run(filler.fill([Gap("erp", "A-1", ""), Gap("material", "M-1", ""), Gap("mail", "fx", "")]))
        self.assertEqual(peak[0], 3)


if __name__ == "__main__":
    unittest.main()
