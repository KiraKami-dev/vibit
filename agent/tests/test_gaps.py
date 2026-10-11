"""The gap-filling second pass: gap detection, merging and the timing guard, on invented facts and fake workers."""
import asyncio
import sys
import time
import unittest
from datetime import date
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
    from datetime import date

    import calc
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
    def test_implausible_base_price_rereads_fx(self):
        o = state([position("1", "A-1")], [requirement("A-1")],
                  [RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1")],
                  {"M-1": complete_record("M-1")})
        problem = "position 1: base price 55000.00 EUR is 1.1e+04 x the ERP scale price 5 USD per piece (FX rate " \
                  "11000 implausible); field left blank"
        self.assertEqual(idents(detect_gaps(o, [problem])), {"mail/fx": ("1",)})

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

    def test_an_empty_catalogue_group_is_no_gap(self):
        research = RequirementResearch(requirement_id="A-1")
        if "no_hits" not in RequirementResearch.model_fields:
            self.skipTest("no no_hits field")
        research.no_hits = True
        o = state([position("1", "A-1")], [requirement("A-1")], [research])
        self.assertEqual(detect_gaps(o), [])

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

    def test_a_settled_no_bid_keeps_only_gaps_that_can_change_it(self):
        found = [Gap("tender", "qa", "not read to its end"), Gap("mail", "approval", "no approval"),
                 Gap("mail", "packaging", "key unresolved", ("1",)), Gap("material", "M-1", "missing", ("1",)),
                 Gap("erp", "A-2", "undecided", ("2",)), Gap("requirement", "A-3", "not read", ("3",)),
                 Gap("mail", "end_reserves", "pending", ("2",))]
        no_bid = Calculation(SimpleNamespace(decision="no_bid", missing_materials=["4"]), {}, [])
        self.assertEqual([g.ident for g in gaps.decisive(found, no_bid)], ["tender/qa", "mail/approval"])
        bid = Calculation(SimpleNamespace(decision="bid", missing_materials=[]), {}, [])
        self.assertEqual(gaps.decisive(found, bid), found)
        # a no_bid without established missing materials (e.g. no positions) keeps every gap
        unclear = Calculation(SimpleNamespace(decision="no_bid", missing_materials=[]), {}, [])
        self.assertEqual(gaps.decisive(found, unclear), found)

    def test_irrelevant_missing_material_facts_are_no_gap(self):
        record = complete_record("M-1").model_copy(update={"pieces_per_carton": None, "allocation_mass_kg": None})
        o = state([position("1", "A-1")], [requirement("A-1")],
                  [RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1")], {"M-1": record})
        self.assertEqual(detect_gaps(o), [])
        o.tender_facts.positions = [position("1", "A-1", packaging_key="P-1")]
        o.mail_facts.packaging = {"P-1": object()}
        self.assertEqual(idents(detect_gaps(o)), {"material/M-1": ("1",)})

    def test_doubtful_mail_facts_become_mail_gaps_on_request(self):
        o = state([], [])
        o.board.post("mail", "fx/USD", "1.1", Source(site="s"), "mails").verdict = "future-dated"
        self.assertNotIn("mail/fx", idents(detect_gaps(o)))
        found = {g.ident: g.reason for g in detect_gaps(o, doubts=True)}
        self.assertTrue(found["mail/fx"].startswith(gaps.DOUBT))

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

    def test_a_round_waits_for_the_verifier_running_beside(self):
        o = runnable(GAP_TAIL + GAP_MIN_ROUND + 60)
        filler = GapFiller(o)
        order = []

        async def verify():
            await asyncio.sleep(0.05)
            order.append("verified")

        async def erp_requirement(rid, reason):
            order.append(rid)

        async def reselect(rids):
            pass

        async def scenario():
            o.verifying = asyncio.create_task(verify())
            filler.erp_requirement, filler.reselect = erp_requirement, reselect
            await filler.run(calc_for(o)(), calc_for(o))

        asyncio.run(scenario())
        self.assertEqual(order[0], "verified")

    def test_a_complete_answer_does_not_wait_for_the_verifier(self):
        o = state([position("1", "A-1")], [requirement("A-1")],
                  [RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1")],
                  {"M-1": complete_record("M-1")})
        o.research_deadline = time.monotonic() + GAP_TAIL + GAP_MIN_ROUND + 60
        o._erp = SimpleNamespace(materials={})

        async def scenario():
            o.verifying = asyncio.create_task(asyncio.sleep(30))
            started = time.monotonic()
            await GapFiller(o).run(calc_for(o)(), calc_for(o))
            o.verifying.cancel()
            return time.monotonic() - started

        self.assertLess(asyncio.run(scenario()), 1.0)

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


class PendingStroke(unittest.TestCase):
    def orchestrator(self):
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.tender_facts, o.mail_facts = None, None
        o.scenario_stamp = date(2026, 6, 1)
        o.requirements = {"A-1": requirement("A-1", "K-1")}

        def material(number, stroke, active=True):
            return complete_record(number).model_copy(update={
                "active": active, "bore": D("50"), "stroke": D(stroke), "pressure": D("160"),
                "mounting": "Flange", "certification": "None"})

        records = [material("M-1", "90"), material("M-2", "110"), material("M-3", "104"),
                   material("M-4", "102", active=False),
                   material("M-5", "103").model_copy(update={"pressure": D("250")})]
        o.erp = ErpResearch(requirements=[], materials={r.material_number: r for r in records})
        return o, RequirementResearch(requirement_id="A-1", inspected=[r.material_number for r in records])

    def test_estimate_is_the_smallest_stroke_above_the_usable_travel(self):
        o, research = self.orchestrator()
        self.assertEqual(o._estimate("A-1", research), "M-3")

    def test_no_estimate_without_a_fitting_material(self):
        o, research = self.orchestrator()
        research.inspected = ["M-1", "M-4", "M-5"]
        self.assertIsNone(o._estimate("A-1", research))



class TenderWatch(unittest.TestCase):
    def run_watch(self, first_seconds: float):
        import orchestrator
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.board, o.trace, o.tender_facts = Board(), Events(), None
        o.research_deadline = time.monotonic() + 600
        attempts = []

        async def tender(site, attempt=""):
            attempts.append(attempt)
            await asyncio.sleep(first_seconds if not attempt else 0.05)
            o.tender_facts = SimpleNamespace(positions=[attempt or "first"])

        o.tender = tender
        saved = orchestrator.TENDER_WATCH_SECONDS
        orchestrator.TENDER_WATCH_SECONDS = 0.1

        async def scenario():
            first = asyncio.create_task(tender("s"))
            await o._tender_watch("s", first)
            return first

        try:
            first = asyncio.run(scenario())
        finally:
            orchestrator.TENDER_WATCH_SECONDS = saved
        return o, attempts, first

    def test_an_attempt_without_positions_is_retried_at_once(self):
        import orchestrator
        from orchestrator import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.board, o.trace, o.tender_facts = Board(), Events(), None
        o.research_deadline = time.monotonic() + 600
        attempts = []

        async def tender(site, attempt=""):
            attempts.append(attempt)
            if attempt:
                o.tender_facts = SimpleNamespace(positions=["01"])

        o.tender = tender

        async def scenario():
            first = asyncio.create_task(tender("s"))
            await o._tender_watch("s", first)

        started = time.monotonic()
        asyncio.run(scenario())
        self.assertLess(time.monotonic() - started, orchestrator.TENDER_WATCH_SECONDS / 10)
        self.assertEqual(attempts, ["", "2"])
        self.assertEqual(o.tender_facts.positions, ["01"])

    def test_a_stalled_tender_gets_a_second_attempt_that_wins(self):
        o, attempts, first = self.run_watch(30)
        self.assertEqual(attempts, ["", "2"])
        self.assertTrue(first.cancelled())
        self.assertEqual(o.tender_facts.positions, ["2"])
        self.assertEqual(o.trace.kinds("gap_filled")[0]["by"], "retry")

    def test_no_second_attempt_when_positions_come_in_time(self):
        o, attempts, _ = self.run_watch(0.01)
        self.assertEqual(attempts, [""])
        self.assertEqual(o.trace.kinds("gap"), [])


class MailReread(unittest.TestCase):
    def test_kept_but_unread_messages_are_read_again_without_browsing(self):
        import research_mail
        o = runnable(GAP_TAIL + 5)
        o.reference = "T-1"
        o.mail_results = {"packaging": SimpleNamespace(kind="K", kept=["view a", "view b"], records=[], stopped=False),
                          "fx": SimpleNamespace(kind="K", kept=[], records=[], stopped=True)}
        filler = GapFiller(o)
        searched = []

        async def mails(kinds):
            searched.extend(kinds)

        async def read_message(view, kind, reference):
            return None if view == "view b" else f"record of {view}"

        filler.mails = mails
        saved = research_mail.read_message
        research_mail.read_message = read_message
        try:
            self.assertEqual(filler.limit(Gap("mail", "packaging", "")), gaps.GAP_TRIES + 1)
            asyncio.run(filler.fill([Gap("mail", "packaging", ""), Gap("mail", "fx", "")]))
        finally:
            research_mail.read_message = saved
        self.assertEqual(o.mail_results["packaging"].records, ["record of view a"])
        self.assertEqual(searched, ["fx"])
        self.assertFalse(filler.unread("packaging"))  # once per kind; next time it is searched


class DoubtsYield(unittest.TestCase):
    def test_doubted_kinds_take_only_spare_mail_tabs(self):
        o = runnable(100)
        o.mail_sessions = [object(), object()]
        filler = GapFiller(o)
        todo = [Gap("mail", "fx", f"{gaps.DOUBT}: header only"), Gap("mail", "setup_pools", "key unresolved"),
                Gap("mail", "approval", f"{gaps.DOUBT}: header only"), Gap("erp", "A-1", "x")]
        self.assertEqual([g.ident for g in filler.spare_doubts(todo)], ["mail/setup_pools", "erp/A-1", "mail/fx"])
        o.mail_sessions = [object()]
        self.assertEqual([g.ident for g in filler.spare_doubts(todo)], ["mail/setup_pools", "erp/A-1"])
        o.mail_sessions = [object()] * 4
        self.assertEqual(filler.spare_doubts([g for g in todo if g.reason.startswith(gaps.DOUBT)]), [])

    def test_doubts_alone_start_no_round(self):
        o = runnable(GAP_TAIL + GAP_MIN_ROUND + 60)
        o.tender_facts.positions = []
        o.plan.needs = []
        o.mail_kinds = [SimpleNamespace(name="fx")]
        o.mail_sessions = [object()]
        o.board.post("mail", "fx/USD", "1.1", Source(site="s"), "mails").verdict = "header only"
        o.gap_rework = True
        o.mail_facts.approval = object()
        filler = GapFiller(o)
        filler.feasible = lambda g: g.area == "mail"
        first = calc_for(o)()
        self.assertIs(asyncio.run(filler.run(first, calc_for(o))), first)
        self.assertEqual(filler.round, 0)


class PositionsReadBound(unittest.TestCase):
    def test_a_hung_thinking_read_falls_back_to_one_quick_read(self):
        import research_tender
        calls = []

        async def read(output_type, instructions, text, prompt, thinking=True):
            calls.append(thinking)
            if thinking:
                await asyncio.sleep(30)
            return "positions"

        saved = research_tender._read, research_tender.POSITIONS_READ_SECONDS
        research_tender._read, research_tender.POSITIONS_READ_SECONDS = read, 0.05
        try:
            started = time.monotonic()
            self.assertEqual(asyncio.run(research_tender.read_positions("text", "T-1")), "positions")
            self.assertLess(time.monotonic() - started, 2)
        finally:
            research_tender._read, research_tender.POSITIONS_READ_SECONDS = saved
        self.assertEqual(calls, [True, False])


class LatePositions(unittest.TestCase):
    def test_positions_from_the_gap_round_reach_the_recalculation_with_fresh_choices(self):
        """Soak run 83438e79: no positions in the first pass, the tender retry found them, but A-2 kept the
        "none" chosen while its stroke reserve was pending and the recalculation became a false no_bid."""
        o = state([], [requirement("A-1"), requirement("A-2")],
                  [RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1"),
                   RequirementResearch(requirement_id="A-2", inspected=["M-2"])],
                  {"M-1": complete_record("M-1"), "M-2": complete_record("M-2")},
                  chosen={"A-1": "M-1", "A-2": None})
        o.research_deadline = time.monotonic() + GAP_TAIL + GAP_MIN_ROUND + 60
        o._erp = SimpleNamespace(materials={})
        o.plan.needs = [n for n in o.plan.needs if n.kind != "mails"]
        filler = GapFiller(o)
        asked = []

        async def tender():
            o.tender_facts.positions = [position("01", "A-1"), position("02", "A-2")]

        async def reselect(rids):
            asked.append(set(rids))
            o.chosen["A-2"] = "M-2"  # the choice made from the facts as they are now

        def calculate():
            # stand-in for build_bid + calc: a requirement without a choice would be a no_bid
            positions = list(o.tender_facts.positions)
            missing = [p.position for p in positions if o.chosen.get(p.requirement) is None]
            result = SimpleNamespace(decision="no_bid" if missing else "bid",
                                     positions=[] if missing else positions)
            return Calculation(result, {}, [])

        filler.tender, filler.reselect = tender, reselect
        o.mail_facts.approval = object()
        best = asyncio.run(filler.run(calculate(), calculate))
        self.assertIn("A-2", asked[0])
        self.assertEqual(best.decision, "bid")
        self.assertEqual([p.position for p in best.result.positions], ["01", "02"])
        self.assertEqual(o.trace.kinds("gap_filled")[0]["gap"], "tender/positions")


class PartialRecords(unittest.TestCase):
    def test_a_partly_read_kind_is_reread_with_a_hint_then_searched(self):
        """Soak run a75f0ec3: the metal index message was read twice with one of its three windows."""
        import dataclasses as dc

        import research_mail
        kind = research_mail.Kind(name="metal_indices", subject="s", sender="x", sender_form="name", site="m",
                                  what="metal index rates", schema=research_mail.SCHEMAS["metal_indices"])
        o = runnable(GAP_TAIL + GAP_MIN_ROUND + 300)
        o.reference = "T-1"
        o.mail_kinds, o.mail_sessions = [kind], [object()]
        o.mail_results = {"metal_indices": SimpleNamespace(kind=kind, kept=["view"], records=["partial"],
                                                           stopped=False)}
        filler = GapFiller(o)
        whats, searched = [], []

        async def read_message(view, k, reference):
            whats.append(k.what)
            return "complete"

        async def mails(kinds):
            searched.append(list(kinds))

        gap = Gap("mail", "metal_indices", "position 1: no metal index rate valid on 2027-01-25")
        filler.mails = mails
        saved = research_mail.read_message
        research_mail.read_message = read_message
        try:
            self.assertEqual(filler.limit(gap), 2)
            asyncio.run(filler.fill([gap]))
            asyncio.run(filler.fill([gap]))
        finally:
            research_mail.read_message = saved
        self.assertEqual(len(whats), 1)
        self.assertIn("no metal index rate valid on 2027-01-25", whats[0])
        self.assertIn("not only the first", whats[0])
        self.assertEqual(o.mail_results["metal_indices"].records, ["partial", "complete"])
        self.assertEqual(searched, [["metal_indices"]])  # the second round searches
        self.assertTrue(dc.is_dataclass(kind))


if __name__ == "__main__":
    unittest.main()
