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
from orchestrator import (  # noqa: E402
    Need,
    Orchestrator,
    Plan,
    answer_from,
    complete_plan,
    rules_text,
    safe_calculate,
    scenario_time,
)

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

    def test_a_failing_pool_leaves_the_other_pool(self):
        heavy = MATERIAL.model_copy(update={"allocation_mass": D("1")})
        good = calc.SetupPool(key="P-A", fee=D("5.00"), members=["1"])
        weightless = calc.SetupPool(key="P-B", fee=D("5.00"), members=["2"])  # no allocation mass: cannot split
        bid = self.bid(position("1", material=heavy), position("2")).model_copy(
            update={"setup_pools": [good, weightless]})
        result, _, problems = safe_calculate(bid)
        self.assertEqual({d.position: d.pool_allocation for d in result.debug}, {"1": D("5.00")})
        self.assertEqual(len(problems), 1)

    def test_missing_carton_size_blanks_packaging_keeps_unit_price(self):
        tariff = calc.PackagingTariff(price_per_carton=D("3"))
        result, blank, _ = safe_calculate(self.bid(position("1", packaging=tariff)))
        self.assertEqual(blank, {"1": {"packaging_per_unit", "unit_price"}})
        answer = answer_from(result, blank)
        self.assertNotIn("packaging_per_unit", answer["positions"][0])
        # the best estimate stays, so the bid form still enters the position
        self.assertEqual(answer["positions"][0]["unit_price"], 10.0)
        self.assertEqual(answer["positions"][0]["base_unit_price"], 10.0)

    def test_missing_metal_index_blanks_surcharge(self):
        heavy = MATERIAL.model_copy(update={"surcharge_mass": D("1")})
        _, blank, _ = safe_calculate(self.bid(position("1", material=heavy)))
        self.assertEqual(blank, {"1": {"surcharge_per_unit", "unit_price"}})

    def test_unpriceable_position_is_omitted(self):
        result, _, problems = safe_calculate(self.bid(position("1"), position("2", conditions=[])))
        self.assertEqual([p.position for p in result.positions], ["1"])
        self.assertTrue(any("position 2 omitted" in p for p in problems))


    def fx_bid(self, rate: str, *positions, **update) -> calc.BidInput:
        return self.bid(*positions).model_copy(update={"fx_rates": {"FX-1": D(rate)}, **update})

    def usd(self, number: str, price: str = "500") -> calc.PositionInput:
        condition = calc.PriceCondition(valid_from=date(2026, 1, 1), currency="USD", fx_key="FX-1", price_unit=100,
                                        scales=[calc.PriceScale(min_quantity=0, price=D(price))])
        return position(number, conditions=[condition])

    def test_implausible_fx_blanks_the_base_price(self):
        # 500 per 100 pieces = 5.00 USD; a rate misread as 11000 gives 55000.00 EUR, 11000 x the ERP price
        result, blank, problems = safe_calculate(self.fx_bid("11000", self.usd("1"), position("2")))
        self.assertEqual(blank, {"1": {"base_unit_price", "unit_price"}})
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("position 1:") and "FX rate" in problems[0], problems[0])
        answer = answer_from(result, blank)
        first, second = answer["positions"]
        self.assertNotIn("base_unit_price", first)
        self.assertIn("unit_price", first)  # the form enters the position only with a unit price
        self.assertEqual(second["base_unit_price"], 10.0)

    def test_plausible_rates_pass(self):
        for rate in ("1.10", "0.0062", "90"):  # a dollar-, a yen- and a dinar-like currency
            _, blank, problems = safe_calculate(self.fx_bid(rate, self.usd("1")))
            self.assertEqual((blank, problems), ({}, []), rate)

    def test_implausible_base_blanks_shared_rebates(self):
        # the bonus band and split of position 2 came from the same, inflated goods value; 3 is not in the plan
        bonus = calc.ProjectBonus(bands=[calc.BonusBand(lower=D("0"), percent=D("1"))], members=["1", "2"])
        result, blank, _ = safe_calculate(self.fx_bid("11000", self.usd("1"), position("2"), position("3"),
                                                      project_bonus=bonus))
        self.assertEqual(blank, {"1": {"base_unit_price", "unit_price", "rebate_total"},
                                 "2": {"rebate_total", "unit_price"}})
        positions = answer_from(result, blank)["positions"]
        self.assertNotIn("rebate_total", positions[1])
        self.assertIn("rebate_total", positions[2])


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
        orchestrator._unknown_clauses(p, SimpleNamespace(packaging=tariff), [pool], None)
        self.assertEqual(orchestrator.blank_hints, {})


class CompletePlan(unittest.TestCase):
    """A plan that dropped a fact kind gets it back: tender and mails from the brief's roles, the rest from the
    websites no other kind holds."""

    IDS = {"bids", "docs", "stock", "inbox"}
    BRIEFING = SimpleNamespace(bid_site="bids", mail_site="inbox")

    def test_missing_kinds_are_filled_in(self):
        plan = Plan(needs=[Need(kind="specification", site="docs", why="rule 2")])
        done, filled = complete_plan(plan, self.BRIEFING, self.IDS)
        self.assertEqual({(n.kind, n.site) for n in done.needs},
                         {("specification", "docs"), ("tender", "bids"), ("qa", "bids"), ("mails", "inbox"),
                          ("materials", "stock")})
        self.assertEqual(sorted(filled), ["mails@inbox", "materials@stock", "qa@bids", "tender@bids"])

    def test_complete_plan_is_unchanged(self):
        plan = Plan(needs=[Need(kind=k, site=s, why="x") for k, s in
                           (("tender", "bids"), ("qa", "docs"), ("specification", "docs"), ("materials", "stock"),
                            ("mails", "inbox"))])
        self.assertEqual(complete_plan(plan, self.BRIEFING, self.IDS), (plan, []))


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
        self.started, self.tasks = [], {}

    def start(self, req):
        self.started.append((req.requirement_id, req.bore))
        self.tasks[req.requirement_id] = SimpleNamespace(done=lambda: True)


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
        self.assertEqual(self.erp.started, [("A-1", D("50")), ("A-1", D("63")), ("A-2", D("50"))])
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

    def test_erp_research_finished_by_the_deadline_is_kept(self):
        from evidence import RequirementResearch

        async def scenario():
            o = bare_orchestrator()
            done = RequirementResearch(requirement_id="A-1", inspected=["M-1"], selected="M-1")

            async def finished():
                return done

            o._erp = SimpleNamespace(tasks={"A-1": asyncio.create_task(finished()),
                                            "A-2": asyncio.create_task(asyncio.sleep(10))}, materials={})
            await asyncio.sleep(0)
            out = o._finished_erp()
            o._erp.tasks["A-2"].cancel()
            return out

        out = asyncio.run(scenario())
        self.assertEqual([r.requirement_id for r in out.requirements], ["A-1"])


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


class MailPlan(unittest.TestCase):
    def test_plan_tries_again_with_a_patient_timeout(self):
        import time

        import orchestrator
        import research_mail
        calls = []

        async def plan(brief, sites, findings="", timeout=0.0):
            calls.append(timeout)
            if len(calls) == 1:
                raise RuntimeError("Request timed out.")
            return ["kind"]

        o = bare_orchestrator()
        o.task, o.mail_deadline = {"brief": "b"}, time.monotonic() + 1000
        original = research_mail.plan_searches
        research_mail.plan_searches = plan
        try:
            self.assertEqual(asyncio.run(o._plan_mail({}, "")), ["kind"])
        finally:
            research_mail.plan_searches = original
        self.assertEqual(calls, [research_mail.PLAN_TIMEOUT, orchestrator.PLAN_PATIENT_TIMEOUT])
        self.assertTrue(any("planning again" in p for p in o.board.problems))


class DeferredHits(unittest.TestCase):
    """_inspect_deferred: hits the ERP stage left unread are read only where the final requirement needs them."""

    def run_with(self, selected: str | None, stroke: str | None):
        from evidence import EffectiveRequirement, MaterialRecord, RequirementResearch
        from research import ErpResearch
        o = bare_orchestrator()
        o.form_left = lambda: 600.0
        rec = MaterialRecord(material_number="M-1", active=True, status="01", bore=D("50"), stroke=D("100"),
                             pressure=D("160"), mounting="Flange", certification="None")
        research = RequirementResearch(requirement_id="A-1", inspected=["M-1", "M-2"], selected=selected)
        o.erp = ErpResearch(requirements=[research], materials={"M-1": rec})
        calls = []

        async def inspect_deferred(req, seconds, every=False):
            calls.append(every)
            return {}
        o._erp = SimpleNamespace(deferred={"A-1": ["M-2"]}, inspect_deferred=inspect_deferred)
        req = EffectiveRequirement(requirement_id="A-1", catalog_group="G-1", bore=D("50"),
                                   stroke=D(stroke) if stroke else None, pressure=D("160"), mounting="Flange",
                                   certification="None", pending=[] if stroke else ["reserve"])
        asyncio.run(o._inspect_deferred([req]))
        return calls

    def test_a_choice_that_holds_reads_nothing(self):
        self.assertEqual(self.run_with("M-1", "100"), [])

    def test_a_choice_that_fails_reads_every_deferred_hit(self):
        self.assertEqual(self.run_with("M-1", "120"), [True])

    def test_a_pending_stroke_reads_the_hits_that_fit_now(self):
        self.assertEqual(self.run_with("M-1", None), [False])


class Events:
    def __init__(self):
        self.events = []

    def event(self, worker, kind, quiet=False, **data):
        self.events.append((kind, data))


def flow_orchestrator() -> Orchestrator:
    o = bare_orchestrator()
    o.trace = Events()
    o.left = o.form_left = lambda: 900.0
    o.plan = Plan(needs=[Need(kind=k, site=s, why="x") for k, s in
                         (("tender", "bids"), ("qa", "bids"), ("specification", "docs"), ("materials", "stock"),
                          ("mails", "inbox"))])
    o.task = {"access": [{"siteId": s} for s in ("bids", "stock", "inbox", "docs")]}
    o.briefing = SimpleNamespace(mail_site="inbox", bid_site="bids")
    o.qa_planned, o.qa_found, o.qa_tried = ["bids"], [], set()
    o.mail_kinds, o.mail_results, o._mail_tabs = [], {}, []
    o.no_bid_settled = None
    o.mail_deadline = 1e12
    return o


class QaElsewhere(unittest.TestCase):
    def test_the_specification_website_first_then_the_others_never_the_mailbox(self):
        o = flow_orchestrator()
        tried = []

        async def qa_read(site, tag):
            tried.append(site)
            if site == "stock":
                o.qa_found.append(site)
                return True
            return False

        o._qa_read = qa_read
        asyncio.run(o._qa_elsewhere({"bids"}))
        self.assertEqual(tried, ["docs", "stock"])
        self.assertEqual(o._qa_sites(), ["stock"])
        tried.clear()
        asyncio.run(o._qa_elsewhere({"bids"}))  # answers found: nothing more is read
        self.assertEqual(tried, [])

    def test_two_callers_do_not_read_one_website_twice(self):
        o = flow_orchestrator()
        tried = []

        async def qa_read(site, tag):
            tried.append(site)
            await asyncio.sleep(0.01)
            return False

        o._qa_read = qa_read

        async def both():
            await asyncio.gather(o._qa_elsewhere({"bids"}), o._qa_elsewhere({"bids"}))

        asyncio.run(both())
        self.assertEqual(sorted(tried), ["docs", "stock"])


def no_bid_result(decision="no_bid", missing=("2",)):
    return SimpleNamespace(decision=decision, missing_materials=list(missing), positions=[])


class EarlyNoBid(unittest.TestCase):
    def run_with(self, recheck=None):
        o = flow_orchestrator()
        o.mail_kinds = [SimpleNamespace(name="approval"), SimpleNamespace(name="packaging")]
        o.mail_results = {"approval": object()}
        o._calculate = lambda: (no_bid_result(), {}, [])

        async def nothing(*args):
            return None

        async def no_rescue(missing):
            return {}

        async def recheck_no_bid(missing):
            return recheck

        o.select_materials = nothing
        o.recheck_no_bid = recheck_no_bid
        o.wording_matches = no_rescue

        async def scenario():
            tab = asyncio.create_task(asyncio.sleep(30))
            o._mail_tabs = [tab]
            await o._early_no_bid()
            await asyncio.sleep(0)
            return tab.cancelled()

        return o, asyncio.run(scenario())

    def test_a_settled_no_bid_ends_the_pricing_mail_searches(self):
        o, cut = self.run_with()
        self.assertTrue(cut)
        self.assertEqual(o.no_bid_settled, ["2"])

    def test_a_recheck_that_brings_a_match_back_keeps_the_mail_searches(self):
        o, cut = self.run_with(recheck=(no_bid_result("bid", ()), {}, []))
        self.assertFalse(cut)
        self.assertIsNone(o.no_bid_settled)


class OmittedEverything(unittest.TestCase):
    def test_the_check_turns_a_bid_without_positions_into_no_bid(self):
        import orchestrator as orch
        o = flow_orchestrator()
        o.tender_facts = SimpleNamespace(positions=[SimpleNamespace(position="1")])
        o.rules, o.verifying = "rules", None
        empty = calc.calculate(calc.BidInput(tender_reference="T-1", scenario_date=date(2026, 6, 1), positions=[]))

        async def nothing(*args):
            return None

        class Filler:
            def __init__(self, _):
                pass

            async def run(self, first, again):
                return orch.Calculation(empty, {}, [], {})

        async def check(proposed, evidence, rules):
            return SimpleNamespace(agrees=False, decision="no_bid", reason="no material for position 1")

        saved = orch.GapFiller, orch.confirm_decision
        orch.GapFiller, orch.confirm_decision = Filler, check
        o.select_materials = nothing
        o.calculate = lambda: None
        try:
            answer, result = asyncio.run(o._answer())
        finally:
            orch.GapFiller, orch.confirm_decision = saved
        self.assertEqual((answer["decision"], answer["positions"], answer["total_net"]), ("no_bid", [], 0.0))


if __name__ == "__main__":
    unittest.main()
