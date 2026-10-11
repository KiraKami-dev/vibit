"""The bid/no_bid decision and the parsers under other sites' wording, on invented facts."""
import asyncio
import sys
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calc  # noqa: E402
import orchestrator as orch  # noqa: E402
from board import Board  # noqa: E402
from evidence import (  # noqa: E402
    Candidate,
    MaterialRecord,
    ParseError,
    Requirement,
    RequirementResearch,
    effective_requirement,
    group_without_hits,
    matches,
    norm_text,
    parse_currency,
    parse_date,
    parse_weeks,
    wording_differences,
)
from research import ErpResearch, reselect  # noqa: E402
from research_tender import (  # noqa: E402
    QaQuantityChange,
    QaRecords,
    QaWithdrawal,
    TenderPosition,
    resolve_tender,
)

SCENARIO = datetime(2026, 6, 1, 12, tzinfo=UTC)
CONDITION = calc.PriceCondition(valid_from=date(2026, 1, 1), scales=[calc.PriceScale(min_quantity=0, price=D("10"))])


def spec(rid: str = "R-1", group: str = "G-1", **update) -> Requirement:
    return Requirement(requirement_id=rid, revision="B", bore=D("40"), stroke=D("80"), pressure=D("10"),
                       mounting="Flansch", certification="keine", catalog_group=group).model_copy(update=update)


def record(number: str = "M-1", **update) -> MaterialRecord:
    return MaterialRecord(material_number=number, active=True, status="A (aktiv)", bore=D("40"), stroke=D("80"),
                          pressure=D("10"), mounting="Flansch", certification="-", free_stock=100,
                          stock_lead_time_weeks=1, production_lead_time_weeks=5,
                          conditions=[CONDITION]).model_copy(update=update)


def tender_position(position: str = "1", requirement: str = "R-1", unit=calc.Unit.PIECE, quantity: int = 10):
    return SimpleNamespace(position=position, requirement=requirement, quantity=quantity, unit=unit,
                           setup_fee=D(0), packaging_key=None, setup_pool_key=None, bonus_key=None)


def orchestrator(positions, requirements, research, materials, chosen=None) -> orch.Orchestrator:
    o = orch.Orchestrator.__new__(orch.Orchestrator)
    o.board, o.trace = Board(), SimpleNamespace(event=lambda *a, **k: None)
    o.reference, o.scenario_stamp = "T-1", date(2026, 6, 1)
    o.mail_facts, o.tender_facts = None, SimpleNamespace(positions=positions, amendments=[])
    o.requirements = {r.requirement_id: r for r in requirements}
    o.erp = ErpResearch(requirements=research, materials=materials)
    o.chosen = dict(chosen or {})
    o.form_left = lambda: 600.0
    return o


# ---------------------------------------------------------------- 1: lead times and reading gaps


class LeadTimes(unittest.TestCase):
    def test_working_days_are_five_a_week(self):
        self.assertEqual(parse_weeks("15 Arbeitstage"), 3)
        self.assertEqual(parse_weeks("10 Werktage"), 2)
        self.assertEqual(parse_weeks("5 business days"), 1)
        self.assertEqual(parse_weeks("14 Tage"), 2)
        self.assertEqual(parse_weeks("4 Wochen"), 4)

    def test_partial_weeks_and_months_are_unknown_not_wrong(self):
        for text in ("7 Arbeitstage", "10 Tage", "2 Monate", "2,5 Wochen"):
            with self.assertRaises(ParseError, msg=text):
                parse_weeks(text)

    def test_missing_lead_time_omits_only_the_position_that_needs_it(self):
        short = record("M-1", stock_lead_time_weeks=None)  # stock covers: from-stock lead time needed, unknown
        other = record("M-2", free_stock=0, stock_lead_time_weeks=None)  # production needed, known
        bid = calc.BidInput(tender_reference="T-1", scenario_date=date(2026, 6, 1), positions=[
            calc.PositionInput(position="1", quantity=5, material=short.to_material(), conditions=[CONDITION]),
            calc.PositionInput(position="2", quantity=5, material=other.to_material(), conditions=[CONDITION])])
        result, _, problems = orch.safe_calculate(bid)
        self.assertEqual(result.decision, "bid")
        self.assertEqual([(p.position, p.lead_time_weeks) for p in result.positions], [("2", 5)])
        self.assertTrue(any("position 1 omitted" in p for p in problems))

    def test_pooled_demand_decides_which_lead_time_is_needed(self):
        material = record("M-1", free_stock=8, production_lead_time_weeks=None).to_material()
        bid = calc.BidInput(tender_reference="T-1", scenario_date=date(2026, 6, 1), positions=[
            calc.PositionInput(position=k, quantity=5, material=material, conditions=[CONDITION]) for k in "12"])
        result, _, problems = orch.safe_calculate(bid)  # alone each fits the stock, together they do not
        self.assertIsNotNone(result)
        self.assertEqual(result.decision, "bid")
        self.assertEqual(len(result.positions), 1)

    def test_reading_gap_never_turns_the_bid_into_no_bid(self):
        o = orchestrator([tender_position("1"), tender_position("2", "R-2", unit="Gebinde")],
                         [spec("R-1"), spec("R-2")],
                         [RequirementResearch(requirement_id="R-1", inspected=["M-1"], selected="M-1"),
                          RequirementResearch(requirement_id="R-2", inspected=["M-2"], selected="M-2")],
                         {"M-1": record("M-1", stock_lead_time_weeks=None, production_lead_time_weeks=None),
                          "M-2": record("M-2")})
        bid, problems = o.build_bid()
        self.assertTrue(all(p.material is not None for p in bid.positions))
        self.assertEqual([p.position for p in bid.positions], ["1"])
        self.assertTrue(any("position 2" in p and "omitted" in p for p in problems))
        result, _, _ = orch.safe_calculate(bid)
        self.assertEqual(result.decision, "bid")  # position 1 cannot be priced: omitted, still a bid

    def test_chosen_material_without_its_record_is_omitted(self):
        o = orchestrator([tender_position("1")], [spec("R-1")],
                         [RequirementResearch(requirement_id="R-1", inspected=["M-9"], selected="M-9")], {})
        bid, problems = o.build_bid()
        self.assertEqual(bid.positions, [])
        self.assertTrue(any("not read" in p for p in problems))


# ---------------------------------------------------------------- 2: an empty catalogue group is no_bid


class EmptyGroup(unittest.TestCase):
    def test_empty_only_with_the_sites_notice_twice_and_no_rows(self):
        notice = {"query": "G-7", "rows": 0, "no_hits": True}
        other = {"query": "Zylinder G-7", "rows": 0, "no_hits": True}
        self.assertTrue(group_without_hits([notice, other], "G-7"))
        self.assertFalse(group_without_hits([notice], "G-7"))  # one try is not enough
        self.assertFalse(group_without_hits([{"query": "G-7", "rows": 0, "no_hits": False}] * 2, "G-7"))
        self.assertFalse(group_without_hits([notice, other, {"query": "x", "rows": 3, "no_hits": False}], "G-7"))
        self.assertFalse(group_without_hits([{"query": "H-1", "rows": 0, "no_hits": True}] * 2, "G-7"))
        self.assertFalse(group_without_hits([], "G-7"))

    def test_empty_group_supports_no_bid(self):
        o = orchestrator([tender_position("1"), tender_position("2", "R-2")], [spec("R-1"), spec("R-2")],
                         [RequirementResearch(requirement_id="R-1", inspected=["M-1"], selected="M-1"),
                          RequirementResearch(requirement_id="R-2", no_hits=True)], {"M-1": record("M-1")})
        bid, _ = o.build_bid()
        self.assertEqual([(p.position, p.material is None) for p in bid.positions], [("1", False), ("2", True)])
        self.assertEqual(calc.calculate(bid).decision, "no_bid")

    def test_search_without_the_notice_is_still_a_gap(self):
        o = orchestrator([tender_position("1")], [spec("R-1")],
                         [RequirementResearch(requirement_id="R-1", problems=["R-1: no material could be inspected"])],
                         {})
        bid, problems = o.build_bid()
        self.assertEqual(bid.positions, [])
        self.assertTrue(any("ERP not researched" in p for p in problems))

    def test_reselect_does_not_ask_for_an_empty_group(self):
        research = RequirementResearch(requirement_id="R-1", no_hits=True)
        want = effective_requirement(spec("R-1"))
        chosen = asyncio.run(reselect([want], ErpResearch(requirements=[research]), None, budget=5))
        self.assertEqual(chosen, {"R-1": None})
        self.assertEqual(research.problems, [])


# ---------------------------------------------------------------- 3: wording is not a mismatch


class Wording(unittest.TestCase):
    def test_negation_phrases_mean_none(self):
        for text in ("keine Zertifizierung", "ohne Zertifikat", "nicht erforderlich", "Keine Anforderung",
                     "not required", "no certification", "entfällt", "-", "n/a", "keine"):
            self.assertEqual(norm_text(text), "none", text)
        for text in ("ATEX", "Flansch", "ohne 2", "nicht rostend", "Norm 3"):
            self.assertNotEqual(norm_text(text), "none", text)

    def test_spec_and_erp_none_wording_match(self):
        want = effective_requirement(spec(certification="keine Zertifizierung"))
        self.assertTrue(matches(want, record(certification="-").attributes()))

    def test_wording_differences_only_for_text_attributes(self):
        want = effective_requirement(spec(mounting="Flansch"))
        self.assertEqual(wording_differences(want, record(mounting="Flange").attributes()), ["mounting"])
        self.assertEqual(wording_differences(want, record(mounting="Flange", bore=D("50")).attributes()), [])
        self.assertEqual(wording_differences(want, record().attributes()), [])

    def test_no_bid_from_wording_is_checked_and_overturned(self):
        o = orchestrator([tender_position("1")], [spec("R-1", mounting="Flansch")],
                         [RequirementResearch(requirement_id="R-1", inspected=["M-1", "M-2"],
                                              candidates=[Candidate(material_number="M-1")])],
                         {"M-1": record("M-1", mounting="Flange"),
                          "M-2": record("M-2", mounting="Flange", bore=D("50"))})
        result, _, _ = o._calculate()
        self.assertEqual(result.decision, "no_bid")
        asked = {}

        async def check(cases):
            asked.update(cases)
            return set(cases)
        with mock.patch.object(orch, "wording_check", check):
            rescued = asyncio.run(o.wording_matches(result.missing_materials))
        self.assertEqual(list(asked), ["R-1/M-1"])  # M-2 differs in a number: never asked
        self.assertEqual(rescued, {"R-1": "M-1"})

    def test_real_difference_keeps_the_no_bid(self):
        o = orchestrator([tender_position("1")], [spec("R-1", certification="ATEX")],
                         [RequirementResearch(requirement_id="R-1", inspected=["M-1"])],
                         {"M-1": record("M-1", certification="-")})

        async def check(cases):
            return set()
        with mock.patch.object(orch, "wording_check", check):
            self.assertEqual(asyncio.run(o.wording_matches(["1"])), {})

    def test_nothing_is_asked_without_a_text_only_candidate(self):
        o = orchestrator([tender_position("1")], [spec("R-1")],
                         [RequirementResearch(requirement_id="R-1", inspected=["M-1"])],
                         {"M-1": record("M-1", bore=D("63"))})

        async def check(cases):
            raise AssertionError("asked")
        with mock.patch.object(orch, "wording_check", check):
            self.assertEqual(asyncio.run(o.wording_matches(["1"])), {})


# ---------------------------------------------------------------- 6 and 12: scoped amendments


class Amendments(unittest.TestCase):
    def amendment(self, revision: str, requirement: str, **attributes) -> calc.Amendment:
        return calc.Amendment(date=date(2026, 5, 1), revision=revision, requirement=requirement,
                              attributes=attributes)

    def test_revision_and_requirement_labels_are_normalised(self):
        for revision, requirement in (("Rev. B", "R-1"), ("Revision B", "Anforderung R-01"), ("b", "r-1")):
            want = effective_requirement(spec(), [self.amendment(revision, requirement, bore=D("50"))])
            self.assertEqual(want.bore, D("50"), (revision, requirement))

    def test_other_revisions_and_requirements_stay_out(self):
        for revision, requirement in (("Rev. C", "R-1"), ("B", "R-2"), ("B-1", "R-1"), ("AB", "R-1")):
            want = effective_requirement(spec(), [self.amendment(revision, requirement, bore=D("50"))])
            self.assertEqual(want.bore, D("40"), (revision, requirement))

    def test_amended_pieces_per_set_is_applied(self):
        req = spec(pieces_per_set=4)
        self.assertEqual(effective_requirement(req).pieces_per_set, 4)
        want = effective_requirement(req, [self.amendment("B", "R-1", pieces_per_set=D("6"))])
        self.assertEqual(want.pieces_per_set, 6)
        o = orchestrator([tender_position("1", unit=calc.Unit.SET, quantity=3)], [req],
                         [RequirementResearch(requirement_id="R-1", inspected=["M-1"], selected="M-1")],
                         {"M-1": record("M-1")})
        o.tender_facts.amendments = [self.amendment("Rev. B", "R-1", pieces_per_set=D("6"))]
        bid, _ = o.build_bid()
        self.assertEqual(calc.calculate(bid).positions[0].quantity, 18)


# ---------------------------------------------------------------- 13: position and answer numbers


class Numbers(unittest.TestCase):
    def test_norm_id(self):
        for a, b in (("Pos. 2", "2"), ("07", "7"), ("Position 02", "2"), ("1.02", "1.2"), ("2​", " 2 "),
                     ("Nr. 5", "5"), ("A-07", "a-7")):
            self.assertEqual(calc.norm_id(a), calc.norm_id(b), (a, b))
        self.assertNotEqual(calc.norm_id("1.2"), calc.norm_id("12"))
        self.assertNotEqual(calc.norm_id("1.2"), calc.norm_id("1.20"))

    def test_quantity_change_and_withdrawal_match_however_numbered(self):
        positions = [TenderPosition(position="02", short_text="x", quantity="10", unit="Stück", requirement="R-1"),
                     TenderPosition(position="3", short_text="y", quantity="5", unit="Stück", requirement="R-1")]
        day = datetime(2026, 5, 1, tzinfo=UTC)
        qa = QaRecords(quantity_changes=[
            QaQuantityChange(answer_number="07", date=day, position="Pos. 2", quantity="12"),
            QaQuantityChange(answer_number="08", date=day, position="Pos. 3", quantity="9")],
            withdrawals=[QaWithdrawal(answer_number="9", date=day, withdrawn_number="8")])
        facts = resolve_tender(positions, qa, SCENARIO)
        self.assertEqual([(p.position, p.quantity) for p in facts.positions], [("02", 12), ("3", 5)])

    def test_resolve_quantity_normalises(self):
        change = calc.QuantityChange(number="A-07", date=date(2026, 5, 1), position="Pos. 01", quantity=4)
        self.assertEqual(calc.resolve_quantity(9, "1", [change], []), 4)
        self.assertEqual(calc.resolve_quantity(9, "1", [change], ["A-7"]), 9)

    def test_commitment_material_number_normalised(self):
        material = calc.Material(material_number="MAT-0042", free_stock=0, stock_lead_time_weeks=1,
                                 production_lead_time_weeks=9)
        pos = calc.PositionInput(position="1", quantity=5, material=material, conditions=[CONDITION],
                                 commitment=calc.DeliveryCommitment(material_number=" mat-42", ceiling=10,
                                                                    lead_time_weeks=3))
        self.assertEqual(calc.lead_time(pos, 5), 3)
        other = pos.model_copy(update={"commitment": pos.commitment.model_copy(update={"material_number": "MAT-0043"})})
        self.assertEqual(calc.lead_time(other, 5), 9)


# ---------------------------------------------------------------- 14: parsers


class Parsers(unittest.TestCase):
    def test_units(self):
        for text in ("Stücke", "Stk.", "ST", "pcs", "Stück", "Teile"):
            self.assertIs(calc.parse_unit(text), calc.Unit.PIECE, text)
        for text in ("Sätze", "Satz", "Set", "Kit"):
            self.assertIs(calc.parse_unit(text), calc.Unit.SET, text)
        with self.assertRaises(calc.CalcError):
            calc.parse_unit("Gebinde")

    def test_unreadable_unit_costs_only_its_position(self):
        positions = [TenderPosition(position="1", short_text="x", quantity="10", unit="Gebinde", requirement="R-1"),
                     TenderPosition(position="2", short_text="y", quantity="5", unit="Stücke", requirement="R-1")]
        qa = QaRecords(quantity_changes=[QaQuantityChange(answer_number="1", date=datetime(2026, 5, 1, tzinfo=UTC),
                                                          position="2", quantity="viele")])
        facts = resolve_tender(positions, qa, SCENARIO)
        self.assertEqual([(p.position, p.quantity) for p in facts.positions], [("2", 5)])
        self.assertEqual(len(facts.problems), 2)

    def test_currency(self):
        for text, code in (("€", "EUR"), ("Euro", "EUR"), ("EUR", "EUR"), ("eur", "EUR"), ("$", "USD"),
                           ("US-Dollar", "USD"), ("USD (US-Dollar)", "USD"), ("£", "GBP"), ("Schweizer Franken", "CHF"),
                           ("", "")):
            self.assertEqual(parse_currency(text), code, text)
        self.assertEqual(parse_currency("Taler"), "TALER")  # unknown: never cut to look like a code

    def test_dates(self):
        for text, day in (("25. Januar 2027", date(2027, 1, 25)), ("3 Mar 2027", date(2027, 3, 3)),
                          ("March 3, 2027", date(2027, 3, 3)), ("1. März 27", date(2027, 3, 1)),
                          ("25.01.27", date(2027, 1, 25)), ("25/01/27", date(2027, 1, 25)),
                          ("2027-01-25", date(2027, 1, 25)), ("21/03 (TT/MM) 2030", date(2030, 3, 21)),
                          ("Dez. 5, 2026", date(2026, 12, 5)), ("5. Mai 2026", date(2026, 5, 5))):
            self.assertEqual(parse_date(text), day, text)
        with self.assertRaises(ParseError):
            parse_date("demnächst")


if __name__ == "__main__":
    unittest.main()
