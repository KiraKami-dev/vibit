"""Tests for the bid entry: the goal built from a plan, the read-back, the time marks and the fallback plan.
All data is invented."""
import re
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent  # noqa: E402
from bidplan import BidPlan, PlanEntry  # noqa: E402
from briefing import Briefing  # noqa: E402
from evidence import Findings, TenderFindings, TenderPosition  # noqa: E402
from submit import (FormSummary, ReadBack, SummaryLine, bid_goal, compare_summary, enterable,  # noqa: E402
                    plan_values)

BRIEFING = Briefing(tender_reference="T-77/A", company_name="Muster Hydraulik GmbH", vat_id="DE000000001",
                    scenario_date="2031-03-04T08:00:00Z", bid_site="portal", mail_site="post", tender_site="portal",
                    spec_site="buyer", erp_site="books", submission_steps="Use the bid form, then sign.")


def mixed_plan() -> BidPlan:
    return BidPlan(tender_reference="T-77/A", discount_percent=Decimal("2.5"), total_net=Decimal("48211.20"),
                   entries=[
                       PlanEntry(position="10", omit=False, material_number="ZX-4411", bore="63", stroke="250",
                                 pressure="210", mounting="Flansch", certification="Norm B", quantity=120,
                                 lead_time_weeks=3, unit_price=Decimal("187.35"), base_unit_price=Decimal("192.15")),
                       PlanEntry(position="20", omit=True, reason="no price condition found"),
                       PlanEntry(position="30", omit=False, material_number="ZX-5512", bore="80", stroke="400",
                                 quantity=96, lead_time_weeks=7, unit_price=Decimal("268.00")),
                       PlanEntry(position="40", omit=False, material_number="ZX-9000", quantity=5),  # no price
                   ])


def line(position: str, **shown) -> SummaryLine:
    return SummaryLine(position=position, omitted=False, **shown)


def matching_summary() -> FormSummary:
    return FormSummary(discount_percent="2,5 %", total_net="48.211,20 EUR", positions=[
        line("Pos. 010", material="ZX-4411 Zylinder", quantity="120 Stück", unit_price="187,35",
             base_unit_price="192,15", lead_time="3 Wochen"),
        SummaryLine(position="20", omitted=True),
        line("30", material="ZX-5512", quantity="96", unit_price="268,00 €", lead_time="7"),
        SummaryLine(position="40", omitted=True)])


class FakeTrace:
    def event(self, *args, **data):
        pass


class FakeSession:
    name = "portal"
    trace = FakeTrace()

    def __init__(self, view: str):
        self.view = view


class TheGoal(unittest.TestCase):
    def test_a_mixed_plan_names_every_filled_value_and_every_omitted_position(self):
        goal = bid_goal(mixed_plan(), BRIEFING, "angebote@muster.example")
        for value in ("T-77/A", "main offer", "ZX-4411", "bore 63", "stroke 250", "pressure 210", "mounting Flansch",
                      "certification Norm B", "quantity 120 pieces", "lead time 3 weeks", "187.35", "187,35",
                      "192.15", "192,15", "ZX-5512", "quantity 96 pieces", "lead time 7 weeks", "268.00", "268,00",
                      "discount 2.5 percent", "48211.20", "48211,20", "angebote@muster.example",
                      "Muster Hydraulik GmbH", "DE000000001", "Use the bid form, then sign."):
            self.assertIn(value, goal)
        self.assertIn("enter exactly these 2 positions", goal)
        self.assertRegex(goal, r"positions 20, 40 and\s+every other position the form shows")
        self.assertIn("never add positions", goal)

    def test_a_position_without_a_price_is_left_out_not_entered(self):
        plan = mixed_plan()
        self.assertFalse(enterable(plan.entries[3]))
        self.assertNotIn("ZX-9000", bid_goal(plan, BRIEFING))

    def test_breakdown_fields_the_plan_lacks_are_left_blank_never_zero(self):
        goal = bid_goal(mixed_plan(), BRIEFING)
        self.assertIn("surcharges, packaging, bonus or rebate", goal)
        self.assertNotRegex(goal, r"\b0[.,]00?\b")
        for sentence in re.split(r"[;\n]", goal):
            if re.search(r"surcharge|packaging|bonus|rebate", sentence):
                self.assertNotRegex(sentence, r"\d")

    def test_without_an_address_the_worker_is_sent_to_the_mailbox_tool(self):
        self.assertIn("bidder_contact_email()", bid_goal(mixed_plan(), BRIEFING))

    def test_a_plan_with_nothing_to_enter_omits_every_position(self):
        goal = bid_goal(agent.fallback_plan("T-77/A", None, "no findings"), BRIEFING)
        self.assertIn("leave every position out", goal)
        self.assertIn("main offer", goal)
        self.assertNotIn("enter exactly these", goal)

    def test_no_bid_declines_without_positions(self):
        plan = BidPlan(tender_reference="T-77/A", decision="no_bid", entries=mixed_plan().entries)
        goal = bid_goal(plan, BRIEFING)
        self.assertIn("decision: no bid", goal)
        self.assertIn("decline or waive", goal)
        self.assertIn("enter no positions at all", goal)
        self.assertIn("net total 0.00", goal)
        self.assertNotIn("ZX-4411", goal)
        self.assertNotIn("main offer", goal)
        self.assertIn("get_signing_tan()", goal)

    def test_plan_values_cover_what_the_worker_types_and_reports(self):
        values = plan_values(mixed_plan())
        for value in ("T-77/A", "ZX-4411", "Flansch", "120", "187.35", "187,35", "48211.20", "48211,20",
                      "48.211,20", "48,211.20", "20", "40"):
            self.assertIn(value, values)
        self.assertNotIn("ZX-9000", values)


class TheReadBack(unittest.TestCase):
    def test_a_summary_that_shows_the_plan_matches(self):
        self.assertEqual(compare_summary(mixed_plan(), matching_summary()), [])

    def test_a_field_the_summary_does_not_show_is_not_compared(self):
        summary = FormSummary(positions=[line("10"), line("30")])
        self.assertEqual(compare_summary(mixed_plan(), summary), [])

    def test_a_different_price_is_named(self):
        summary = matching_summary()
        summary.positions[0].unit_price = "178,35"
        (difference,) = compare_summary(mixed_plan(), summary)
        self.assertEqual((difference.kind, difference.position), ("wrong", "Pos. 010"))
        self.assertIn("unit price shows '178,35', the bid says 187.35", difference.text)

    def test_a_position_the_plan_leaves_out_is_extra(self):
        summary = matching_summary()
        summary.positions[1] = line("20", material="ZX-1", quantity="4", unit_price="1,00")
        summary.positions.append(line("50", quantity="1"))
        self.assertEqual([(d.kind, d.position) for d in compare_summary(mixed_plan(), summary)],
                         [("extra", "20"), ("extra", "50")])

    def test_a_planned_position_left_out_and_a_wrong_total(self):
        summary = matching_summary()
        summary.positions[2] = SummaryLine(position="30", omitted=True)
        summary.total_net = "22.482,00"
        self.assertEqual([d.kind for d in compare_summary(mixed_plan(), summary)], ["missing", "order"])
        del summary.positions[2]
        self.assertEqual([d.kind for d in compare_summary(mixed_plan(), summary)], ["missing", "order"])

    def test_no_bid_accepts_no_entered_position(self):
        plan = BidPlan(tender_reference="T-77/A", decision="no_bid")
        self.assertEqual(compare_summary(plan, FormSummary(total_net="0,00")), [])
        self.assertEqual([d.kind for d in compare_summary(plan, FormSummary(positions=[line("10", quantity="3")]))],
                         ["extra"])

    def test_a_mismatch_is_corrected_once_then_the_position_is_omitted(self):
        summary = matching_summary()
        summary.positions[0].unit_price = "178,35"
        check = ReadBack(mixed_plan(), FakeSession(summary.model_dump_json()), time.monotonic() + 900)
        first = check.check(summary)
        self.assertIn("unit price shows '178,35'", first)
        self.assertIn("report_summary() again", first)
        self.assertFalse(check.done)
        second = check.check(summary)
        self.assertIn("Leave out position Pos. 010", second)
        self.assertTrue(check.done)

    def test_a_bid_that_only_holds_less_than_the_plan_is_signed(self):
        summary = matching_summary()
        summary.positions[2] = SummaryLine(position="30", omitted=True)
        check = ReadBack(mixed_plan(), FakeSession(summary.model_dump_json()), time.monotonic() + 900, corrections=0)
        self.assertIn("sign it as it is", check.check(summary))
        self.assertTrue(check.done)

    def test_without_time_a_mismatch_is_signed_not_corrected(self):
        summary = matching_summary()
        summary.positions[0].unit_price = "178,35"
        check = ReadBack(mixed_plan(), FakeSession(summary.model_dump_json()), time.monotonic() + 60)
        self.assertIn("sign it as it is", check.check(summary))

    def test_a_report_that_is_not_on_the_page_is_refused(self):
        check = ReadBack(mixed_plan(), FakeSession("Schritt 2: Positionen"), time.monotonic() + 900)
        answer = check.check(matching_summary())
        self.assertIn("are not on the page you are on", answer)
        self.assertFalse(check.done)

    def test_a_matching_summary_opens_the_way_to_the_tan(self):
        summary = matching_summary()
        check = ReadBack(mixed_plan(), FakeSession(summary.model_dump_json()), time.monotonic() + 900)
        self.assertIn("matches", check.check(summary))
        self.assertTrue(check.done)


class ThePlanAndTheClock(unittest.TestCase):
    FINDINGS = Findings(tender=TenderFindings(positions=[TenderPosition(position="10", quantity="4", unit="Stück"),
                                                         TenderPosition(position="20", quantity="2", unit="Satz")]))

    def test_when_planning_fails_every_known_position_is_omitted(self):
        with patch.object(agent, "build_plan", side_effect=NotImplementedError):
            plan = agent.make_plan(self.FINDINGS, BRIEFING, "angebote@muster.example")
        self.assertEqual((plan.decision, plan.tender_reference, plan.total_net), ("bid", "T-77/A", Decimal("0.00")))
        self.assertEqual([(entry.position, entry.omit) for entry in plan.entries], [("10", True), ("20", True)])
        self.assertIn("NotImplementedError", plan.entries[0].reason)

    def test_without_findings_the_plan_omits_whatever_the_form_shows(self):
        with patch.object(agent, "build_plan") as build:
            for nothing in (None, Findings()):
                plan = agent.make_plan(nothing, BRIEFING, "")
                self.assertEqual((plan.decision, plan.entries), ("bid", []))
        build.assert_not_called()

    def test_the_plan_gets_the_scenario_time_and_the_mailbox_domain(self):
        with patch.object(agent, "build_plan", return_value=BidPlan(tender_reference="other")) as build:
            plan = agent.make_plan(self.FINDINGS, BRIEFING, "Angebote@Muster.Example")
        build.assert_called_once_with(self.FINDINGS, "T-77/A", datetime(2031, 3, 4, 8, tzinfo=timezone.utc),
                                      "muster.example")
        self.assertEqual(plan.tender_reference, "T-77/A")

    def test_an_unreadable_scenario_date_falls_back(self):
        briefing = BRIEFING.model_copy(update={"scenario_date": "next spring"})
        with patch.object(agent, "build_plan") as build:
            plan = agent.make_plan(self.FINDINGS, briefing, "a@b.example")
        build.assert_not_called()
        self.assertTrue(all(entry.omit for entry in plan.entries))

    def test_scenario_times_are_timezone_aware(self):
        self.assertEqual(agent.scenario_time("2031-03-04T08:00:00Z"), datetime(2031, 3, 4, 8, tzinfo=timezone.utc))
        self.assertEqual(agent.scenario_time("2031-03-04 08:00 +02:00").utcoffset(), timedelta(hours=2))
        self.assertEqual(agent.scenario_time("4.3.2031, 08:30 Uhr"), datetime(2031, 3, 4, 8, 30, tzinfo=timezone.utc))
        self.assertEqual(agent.scenario_time("04.03.2031 08:30 (Europe/Berlin)").utcoffset(), timedelta(hours=1))
        self.assertEqual(agent.scenario_time("2031-03-04"), datetime(2031, 3, 4, tzinfo=timezone.utc))

    def test_the_marks_leave_time_to_enter_and_sign(self):
        research_by, entry_by, sign_by = agent.marks(0.0, 20 * 60)
        self.assertEqual((research_by, entry_by, sign_by), (420.0, 450.0, 1110.0))
        research_by, entry_by, sign_by = agent.marks(0.0, 5 * 60)
        self.assertLessEqual(research_by, entry_by)
        self.assertGreaterEqual(sign_by - entry_by, agent.MIN_ENTRY_SECONDS)


if __name__ == "__main__":
    unittest.main()
