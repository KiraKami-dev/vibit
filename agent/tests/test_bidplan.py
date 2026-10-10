"""Findings to bid plan on invented facts; expected values are worked out by hand in the comments."""
import sys
import unittest
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bidplan import build_plan  # noqa: E402
from evidence import (  # noqa: E402
    Amendment,
    Approval,
    Candidate,
    Condition,
    ErpFindings,
    Findings,
    FxFixing,
    MailFindings,
    MaterialFacts,
    QuantityChange,
    Requirement,
    Scale,
    SpecFindings,
    TechnicalRelease,
    TenderFindings,
    TenderPosition,
    Withdrawal,
)

REF = "T-0000"
DOMAIN = "example.test"
DAY = datetime(2026, 5, 15, tzinfo=UTC)  # a bare day: records dated on it still apply


def tpos(position="1", quantity="20", unit="Stück", requirement="R1"):
    return TenderPosition(position=position, quantity=quantity, unit=unit, requirement=requirement)


def req(name="R1", bore="50 mm", **kw):
    fields = {"stroke": "200 mm", "pressure": "160 bar", "mounting": "Flansch", "certification": "X1"} | kw
    return Requirement(requirement=name, bore=bore, **fields)


def cand(number="M-1", bore="50", active=True, **kw):
    fields = {"stroke": "200,0", "pressure": "160", "mounting": "FLANSCH", "certification": "X1"} | kw
    return Candidate(material_number=number, bore=bore, status="shown status", active=active, **fields)


def cond(*scales, start="2026-01-01", end="", **kw):
    return Condition(valid_from=start, valid_to=end, scales=[Scale(min_quantity=q, price=p) for q, p in scales],
                     **({"currency": "EUR", "price_unit": "1"} | kw))


def facts(number="M-1", stock="0", conditions=None, stock_weeks="2", production_weeks="8 Wochen"):
    return MaterialFacts(material_number=number, free_stock=stock, stock_lead_time_weeks=stock_weeks,
                         production_lead_time_weeks=production_weeks, number_format="de",
                         conditions=[cond(("1", "10,00"))] if conditions is None else conditions)


def erp(requirement="R1", chosen="M-1", candidates=None, material=None, all_read=True):
    return ErpFindings(requirement=requirement, chosen=chosen, all_candidates_read=all_read,
                       candidates=[cand()] if candidates is None else candidates,
                       facts=facts() if material is None else material)


def approval(percent="5 %", minimum="", date="2026-05-01T10:00:00Z", reference=REF, sender=f"leitung@{DOMAIN}", **kw):
    return Approval(reference=reference, sender=sender, date=date, approves=True, percent=percent,
                    min_order_value=minimum, number_format="de", **kw)


def fixing(rates, date="2026-05-02T08:00:00Z", reference=REF, sender=f"finanzen@{DOMAIN}", sent=True):
    return FxFixing(reference=reference, sender=sender, date=date, rates=rates, sent=sent, number_format="de")


def findings(positions=None, requirements=None, erps=None, approvals=(), fixings=(), changes=(), withdrawals=(),
             amendments=(), revision="B", complete=True):
    erps = [erp()] if erps is None else erps
    return Findings(
        tender=TenderFindings(positions=[tpos()] if positions is None else positions, quantity_changes=list(changes),
                              withdrawals=list(withdrawals), amendments=list(amendments), complete=complete),
        spec=SpecFindings(current_revision=revision, requirements=[req()] if requirements is None else requirements,
                          complete=complete),
        erp={e.requirement: e for e in erps},
        mail=MailFindings(approvals=list(approvals), fx_fixings=list(fixings)))


def plan(found, day=DAY):
    return build_plan(found, REF, day, DOMAIN)


def entry(found, position="1", day=DAY):
    return next(e for e in plan(found, day).entries if e.position == position)


def two_materials(second_conditions=None, **kw):
    """Position A: 10 pieces of M-1 at 10,00; position B: 5 pieces of M-2 at 20,00 (or the given conditions)."""
    second = [cond(("1", "20,00"))] if second_conditions is None else second_conditions
    return findings(
        positions=[tpos("A", "10"), tpos("B", "5", requirement="R2")], requirements=[req(), req("R2", bore="63")],
        erps=[erp(), erp("R2", "M-2", [cand("M-2", bore="63")], facts("M-2", conditions=second))], **kw)


class Filled(unittest.TestCase):
    def test_complete_position(self):
        # 20 pieces reach the scale from 10 -> 9,50; 5 % -> 9.025 -> 9.03; no stock -> production 8 weeks
        found = findings(erps=[erp(material=facts(conditions=[cond(("1", "10,00"), ("10", "9,50"))]))],
                         approvals=[approval()])
        got = plan(found)
        e = got.entries[0]
        self.assertEqual((e.omit, e.material_number, e.quantity, e.lead_time_weeks), (False, "M-1", 20, 8))
        self.assertEqual((e.base_unit_price, e.unit_price), (D("9.50"), D("9.03")))
        # the material's attributes as the ERP writes them
        self.assertEqual((e.bore, e.stroke, e.pressure, e.mounting, e.certification),
                         ("50", "200,0", "160", "FLANSCH", "X1"))
        # total 20 x 9.03 = 180.60
        self.assertEqual((got.decision, got.discount_percent, got.total_net), ("bid", D(5), D("180.60")))
        self.assertTrue(any("not applied yet" in n for n in got.notes))

    def test_total_is_sum_of_rounded_lines(self):
        # A: 20 x 9.50, 5 % -> 9.03 -> 180.60; B: 7 x 3.33, 5 % -> 3.1635 -> 3.16 -> 22.12; total 202.72
        found = findings(
            positions=[tpos("A", "20"), tpos("B", "7", requirement="R2")], requirements=[req(), req("R2", bore="63")],
            erps=[erp(material=facts(conditions=[cond(("1", "9,50"))])),
                  erp("R2", "M-2", [cand("M-2", bore="63")], facts("M-2", conditions=[cond(("1", "3,33"))]))],
            approvals=[approval()])
        got = plan(found)
        self.assertEqual([e.unit_price for e in got.entries], [D("9.03"), D("3.16")])
        self.assertEqual(got.total_net, sum(e.quantity * e.unit_price for e in got.entries))
        self.assertEqual(got.total_net, D("202.72"))

    def test_entries_follow_the_tender_order(self):
        found = two_materials()
        found.tender.positions.reverse()
        self.assertEqual([e.position for e in plan(found).entries], ["B", "A"])


class Quantity(unittest.TestCase):
    """Rules 1, 13, 17, 21."""

    def change(self, answer, quantity, date, **kw):
        return QuantityChange(answer=answer, position="1", quantity=quantity, date=date, **kw)

    def test_latest_change_not_withdrawn_not_future(self):
        changes = [self.change("A1", "30", "2026-04-01T09:00:00Z"), self.change("A2", "40", "2026-04-10T09:00:00Z"),
                   self.change("A3", "99", "2026-06-01T09:00:00Z"),
                   QuantityChange(answer="A4", position="2", quantity="77", date="2026-04-11T09:00:00Z")]
        # A3 is future-dated, A4 is another position -> A2 = 40
        self.assertEqual(entry(findings(changes=changes)).quantity, 40)
        # A2 withdrawn before the scenario date -> A1 = 30
        gone = [Withdrawal(answer="A5", withdraws="A2", date="2026-04-20T09:00:00Z")]
        self.assertEqual(entry(findings(changes=changes, withdrawals=gone)).quantity, 30)
        # a withdrawal dated after the scenario date does not apply -> still 40
        later = [Withdrawal(answer="A5", withdraws="A2", date="2026-05-16T00:00:01Z")]
        self.assertEqual(entry(findings(changes=changes, withdrawals=later)).quantity, 40)
        # both withdrawn -> the tender quantity 20
        both = gone + [Withdrawal(answer="A6", withdraws="A1", date="2026-04-21T09:00:00Z")]
        self.assertEqual(entry(findings(changes=changes, withdrawals=both)).quantity, 20)

    def test_change_time_in_another_zone(self):
        # 16.05. 01:30 at +02:00 is 15.05. 23:30 UTC: on the scenario day; 02:30 would be 00:30 UTC the day after
        on_day = self.change("A1", "30", "16.05.2026 01:30", date_order="dmy", timezone="+02:00")
        after = self.change("A1", "30", "16.05.2026 02:30", date_order="dmy", timezone="+02:00")
        self.assertEqual(entry(findings(changes=[on_day])).quantity, 30)
        self.assertEqual(entry(findings(changes=[after])).quantity, 20)
        # a scenario with a time of day is a cut-off: 23:30 UTC is after 12:00 UTC
        noon = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
        self.assertEqual(entry(findings(changes=[on_day]), day=noon).quantity, 20)

    def test_change_with_unreadable_date_is_skipped(self):
        bad = self.change("A1", "30", "03/04/2026")  # March or April: order not stated
        got = plan(findings(changes=[bad]))
        self.assertEqual(got.entries[0].quantity, 20)
        self.assertTrue(any("quantity change A1 skipped" in n for n in got.notes))

    def test_sets_to_pieces(self):
        # 3 Satz x 4 pieces per set = 12 pieces; the scale from 10 pieces applies
        found = findings(positions=[tpos(quantity="3", unit="Satz")], requirements=[req(pieces_per_set="4")],
                         erps=[erp(material=facts(conditions=[cond(("1", "10,00"), ("10", "9,00"))]))])
        e = entry(found)
        self.assertEqual((e.quantity, e.unit_price), (12, D("9.00")))
        # pieces per set unknown -> omitted
        e = entry(findings(positions=[tpos(quantity="3", unit="Satz")]))
        self.assertTrue(e.omit)
        self.assertIn("pieces per set", e.reason)

    def test_ambiguous_quantity_omits_only_that_position(self):
        found = two_materials()
        found.tender.positions[0].quantity = "1.234"  # 1234 or 1.234: number format not stated
        got = plan(found)
        self.assertEqual([e.omit for e in got.entries], [True, False])
        self.assertIn("ambiguous number", got.entries[0].reason)
        # only B counts: 5 x 20.00 = 100.00
        self.assertEqual(got.total_net, D("100.00"))


class Material(unittest.TestCase):
    """Rules 3, 8, 14, 20."""

    def amended(self, revision):
        # candidates differ in stroke only: M-1 has 200, M-2 has 250
        return findings(erps=[erp(chosen="M-2", candidates=[cand(), cand("M-2", stroke="250")],
                                  material=facts("M-2"))],
                        amendments=[Amendment(answer="A1", revision=revision, requirement="R1",
                                              attributes={"stroke": "250 mm"}, date="2026-04-01T09:00:00Z")])

    def test_amendment_for_the_current_revision(self):
        e = entry(self.amended("B"))
        self.assertEqual((e.omit, e.material_number, e.stroke), (False, "M-2", "250"))

    def test_amendment_for_another_revision_ignored(self):
        # stroke stays 200, so M-2 fails the cross-check; M-1 is the only active match and replaces it,
        # but the facts read belong to M-2 -> omitted
        got = plan(self.amended("A"))
        self.assertTrue(got.entries[0].omit)
        self.assertIn("no stock and price facts for material M-1", got.entries[0].reason)
        self.assertTrue(any("only active candidate that matches" in n for n in got.notes))

    def test_future_amendment_ignored(self):
        found = findings(amendments=[Amendment(answer="A1", revision="B", requirement="R1",
                                               attributes={"stroke": "250"}, date="2026-05-16T08:00:00Z")])
        self.assertEqual(entry(found).material_number, "M-1")

    def test_usable_stroke_without_release_omitted(self):
        e = entry(findings(requirements=[req(usable_stroke=True, reserve_key="K1")]))
        self.assertTrue(e.omit)
        self.assertIn("usable travel", e.reason)

    def usable(self, *releases):
        # usable 176 + 2 x 12 = the candidate's nominal 200
        found = findings(requirements=[req(stroke="176 mm", usable_stroke=True, reserve_key="K1")])
        found.mail.technical_releases = list(releases)
        return found

    def release(self, reserve="12 mm", revision="Rev. B", date="2026-05-01T10:00:00Z", sender=f"technik@{DOMAIN}",
                reference=REF):
        return TechnicalRelease(reference=reference, sender=sender, date=date, revision=revision,
                                reserves={"K1": reserve})

    def test_usable_stroke_derived(self):
        e = entry(self.usable(self.release()))
        self.assertEqual((e.omit, e.material_number), (False, "M-1"))
        # the latest release of the current revision governs; other revisions, senders, references and future
        # ones do not
        ignored = (self.release("20 mm", revision="A", date="2026-05-10T10:00:00Z"),
                   self.release("20 mm", sender=f"vertrieb@{DOMAIN}", date="2026-05-10T10:00:00Z"),
                   self.release("20 mm", reference=REF + "-X", date="2026-05-10T10:00:00Z"),
                   self.release("20 mm", date="2026-05-16T08:00:00Z"))
        self.assertEqual(entry(self.usable(self.release("5 mm", date="2026-04-01T10:00:00Z"), self.release(),
                                           *ignored)).material_number, "M-1")
        # a newer governing release replaces the older one: 176 + 2 x 5 matches nothing
        self.assertTrue(entry(self.usable(self.release(), self.release("5 mm", date="2026-05-02T10:00:00Z"))).omit)

    def test_release_without_the_key_omits(self):
        found = self.usable(self.release())
        found.spec.requirements[0].reserve_key = "K2"
        self.assertIn("no end reserve for key K2", entry(found).reason)

    def test_chosen_material_fails_cross_check(self):
        # wrong pressure, and the list was not read to the end: omit, no no_bid
        found = findings(erps=[erp(candidates=[cand(pressure="250")], all_read=False)])
        got = plan(found)
        self.assertEqual((got.decision, got.entries[0].omit), ("bid", True))
        self.assertIn("does not match", got.entries[0].reason)
        # blocked
        e = entry(findings(erps=[erp(candidates=[cand(active=False)], all_read=False)]))
        self.assertIn("not active", e.reason)
        # not listed at all
        e = entry(findings(erps=[erp(chosen="M-9", candidates=[cand(pressure="250")], all_read=False)]))
        self.assertIn("not among the candidates", e.reason)

    def test_single_other_match_is_used(self):
        found = findings(erps=[erp(chosen="M-2", candidates=[cand("M-2", bore="63"), cand()])])
        got = plan(found)
        self.assertEqual((got.entries[0].omit, got.entries[0].material_number), (False, "M-1"))
        self.assertTrue(any("M-2 fails the cross-check" in n for n in got.notes))

    def test_wording_is_the_models_call(self):
        # the sources word "no certification" differently: the chosen material equals every number and is taken
        found = findings(requirements=[req(certification="none required")],
                         erps=[erp(candidates=[cand(certification="None")])])
        got = plan(found)
        self.assertEqual((got.decision, got.entries[0].omit, got.entries[0].material_number), ("bid", False, "M-1"))
        self.assertTrue(any("worded differently" in n for n in got.notes))
        # a chosen material never makes a no_bid, even when its text differs and nothing else matches
        found = findings(requirements=[req(certification="none required")],
                         erps=[erp(candidates=[cand(certification="None", pressure="250")])])
        self.assertEqual(plan(found).decision, "bid")

    def no_match(self, **kw):
        # position 1 is fine; position 2 only has a blocked match and an active one with another mounting
        return findings(
            positions=[tpos(), tpos("2", "5", requirement="R2")], requirements=[req(), req("R2", bore="63")],
            erps=[erp(), erp("R2", "", [cand("M-2", bore="63", active=False),
                                        cand("M-3", bore="63", mounting="Gabel")], facts("M-3"),
                             all_read=kw.pop("all_read", True))],
            approvals=[approval(minimum="1,00")], **kw)

    def test_no_bid_when_certain(self):
        got = plan(self.no_match())
        # conditional approval and no order value -> 0 %
        self.assertEqual((got.decision, got.total_net, got.discount_percent), ("no_bid", D("0.00"), D(0)))
        self.assertEqual([e.omit for e in got.entries], [True, True])
        self.assertTrue(any(n.startswith("no_bid: position 2") for n in got.notes))

    def test_not_certain_omits_instead(self):
        for uncertain in (self.no_match(all_read=False), self.no_match(complete=False)):
            got = plan(uncertain)
            self.assertEqual((got.decision, [e.omit for e in got.entries]), ("bid", [False, True]))
            # position 1 alone: 20 x 10.00 = 200.00 reaches the minimum 1.00 -> 5 % -> 9.50 -> 190.00
            self.assertEqual(got.total_net, D("190.00"))
        # a candidate whose status is unknown might still match
        found = self.no_match()
        found.erp["R2"].candidates[0].active = None
        self.assertEqual(plan(found).decision, "bid")
        # so might one whose attribute could not be read
        found = self.no_match()
        found.erp["R2"].candidates[1].mounting = ""
        self.assertEqual(plan(found).decision, "bid")


class LeadTime(unittest.TestCase):
    """Rules 6, 15."""

    def shared(self, stock, second_conditions=None):
        # positions of 20 and 15 pieces select M-1 through two requirements: pooled demand 35
        second = [cond(("1", "10,00"), ("30", "9,00"))] if second_conditions is None else second_conditions
        return findings(
            positions=[tpos(), tpos("2", "15", requirement="R2")], requirements=[req(), req("R2")],
            erps=[erp(material=facts(stock=stock, conditions=[cond(("1", "10,00"), ("30", "9,00"))])),
                  erp("R2", material=facts(stock=stock, conditions=second))])

    def test_pooled_stock(self):
        short = plan(self.shared("30"))  # 30 < 35 -> production for both, though 30 covers each alone
        self.assertEqual([e.lead_time_weeks for e in short.entries], [8, 8])
        enough = plan(self.shared("35"))  # 35 >= 35 -> stock for both
        self.assertEqual([e.lead_time_weeks for e in enough.entries], [2, 2])

    def test_scale_per_position(self):
        # 20 and 15 pieces each stay below the scale from 30, although the material sum is 35
        self.assertEqual([e.unit_price for e in plan(self.shared("35")).entries], [D("10.00"), D("10.00")])

    def test_unpriced_position_still_counts_in_the_pool(self):
        got = plan(self.shared("30", second_conditions=[]))
        self.assertEqual([e.omit for e in got.entries], [False, True])
        self.assertIn("no price condition", got.entries[1].reason)
        self.assertEqual(got.entries[0].lead_time_weeks, 8)  # 30 < 20 + 15

    def test_unknown_stock_omits(self):
        e = entry(findings(erps=[erp(material=facts(stock=""))]))
        self.assertTrue(e.omit)
        self.assertIn("free stock", e.reason)


class Price(unittest.TestCase):
    """Rules 4, 22."""

    def test_condition_valid_on_the_scenario_day(self):
        conditions = [cond(("1", "8,00"), start="01.01.2025", end="31.12.2025", date_order="dmy"),
                      cond(("1", "9,00"), start="01.01.2026", end="15.05.2026", date_order="dmy"),  # inclusive end
                      cond(("1", "11,00"), start="16.05.2026", date_order="dmy")]
        self.assertEqual(entry(findings(erps=[erp(material=facts(conditions=conditions))])).base_unit_price, D("9.00"))
        e = entry(findings(erps=[erp(material=facts(conditions=conditions[:1]))]))
        self.assertTrue(e.omit)
        self.assertIn("no price condition valid", e.reason)

    def foreign(self, **kw):
        # 1.234,56 per 100 pieces = 12.3456 per piece x 0.9 EUR per unit = 11.11104 -> 11.11
        c = cond(("1", "1.234,56"), currency="USD", fx_key="FX-1", price_unit="100 ST")
        return findings(erps=[erp(material=facts(conditions=[c]))], **kw)

    def test_price_unit_and_fx(self):
        got = plan(self.foreign(fixings=[fixing({"FX-1": "0,9"})]))
        e = got.entries[0]
        # total 20 x 11.11 = 222.20
        self.assertEqual((e.base_unit_price, e.unit_price, got.total_net), (D("11.11"), D("11.11"), D("222.20")))

    def test_price_unit_in_eur(self):
        # 250,00 per 100 pieces = 2.50
        c = cond(("1", "250,00"), price_unit="100")
        self.assertEqual(entry(findings(erps=[erp(material=facts(conditions=[c]))])).base_unit_price, D("2.50"))

    def test_latest_authoritative_fixing(self):
        fixings = [fixing({"FX-1": "0,8"}, date="2026-04-01T08:00:00Z"),
                   fixing({"FX-1": "0,9"}, date="2026-05-02T08:00:00Z"),
                   fixing({"FX-1": "0,5"}, date="2026-05-03T08:00:00Z", sent=False),  # draft
                   fixing({"FX-1": "0,6"}, date="2026-05-04T08:00:00Z", reference=REF + "-A"),  # look-alike
                   fixing({"FX-1": "0,7"}, date="2026-05-05T08:00:00Z", sender="finanzen@example.invalid"),
                   fixing({"FX-1": "0,4"}, date="2026-05-20T08:00:00Z")]  # future
        self.assertEqual(entry(self.foreign(fixings=fixings)).base_unit_price, D("11.11"))

    def test_missing_fx_omits(self):
        for fixings in ([], [fixing({"FX-1": "0,9"}, sent=False)], [fixing({"FX-2": "0,9"})],
                        [fixing({"FX-1": "0,9"}, sender=f"leitung@{DOMAIN}")]):
            got = plan(self.foreign(fixings=fixings))
            self.assertTrue(got.entries[0].omit)
            self.assertIn("FX", got.entries[0].reason)
            self.assertEqual(got.total_net, D("0.00"))


class Discount(unittest.TestCase):
    """Rules 5, 16, 21."""

    def test_only_the_authoritative_approval(self):
        approvals = [approval("3 %", date="2026-04-01T10:00:00Z"), approval("5 %", date="2026-05-01T10:00:00Z"),
                     approval("9 %", date="2026-05-02T10:00:00Z", reference=REF + "-A"),  # look-alike reference
                     approval("8 %", date="2026-05-03T10:00:00Z", sender="leitung@example.invalid"),
                     approval("7 %", date="2026-05-04T10:00:00Z", sender=f"technik@{DOMAIN}"),
                     approval("6 %", date="2026-05-16T10:00:00Z")]  # future
        self.assertEqual(plan(findings(approvals=approvals)).discount_percent, D(5))
        # a message that does not approve anything is no approval
        declined = Approval(reference=REF, sender=f"leitung@{DOMAIN}", date="2026-05-10T10:00:00Z", approves=False)
        self.assertEqual(plan(findings(approvals=approvals + [declined])).discount_percent, D(5))
        self.assertEqual(plan(findings()).discount_percent, D(0))

    def test_threshold_inclusive(self):
        # order value 10 x 10.00 + 5 x 20.00 = 200.00 >= 200.00 -> 10 %: 9.00 and 18.00, total 180.00
        got = plan(two_materials(approvals=[approval("10", minimum="200,00 EUR")]))
        self.assertEqual((got.discount_percent, got.total_net), (D(10), D("180.00")))
        self.assertEqual([e.unit_price for e in got.entries], [D("9.00"), D("18.00")])

    def test_threshold_failed_never_revives_an_older_approval(self):
        # 200.00 < 200.01 -> 0 %, although an older unconditional approval exists
        approvals = [approval("4", date="2026-04-01T10:00:00Z"), approval("10", minimum="200,01")]
        got = plan(two_materials(approvals=approvals))
        self.assertEqual((got.discount_percent, got.total_net), (D(0), D("200.00")))

    def test_unknown_base_price_with_conditional_approval(self):
        # B has no price: the known part is 10 x 10.00 = 100.00
        reached = plan(two_materials(second_conditions=[], approvals=[approval("10", minimum="100,00")]))
        self.assertEqual((reached.discount_percent, reached.total_net), (D(10), D("90.00")))
        short = plan(two_materials(second_conditions=[], approvals=[approval("10", minimum="150,00")]))
        self.assertEqual((short.discount_percent, short.total_net), (D(0), D("100.00")))
        self.assertTrue(any("some base prices are unknown" in n for n in short.notes))

    def test_unreadable_mail_is_skipped_with_a_note(self):
        # the later approval's date could be March or April -> skipped; the older one governs
        approvals = [approval("5"), approval("9", date="03/04/2026")]
        got = plan(findings(approvals=approvals))
        self.assertEqual(got.discount_percent, D(5))
        self.assertTrue(any("approval mail skipped" in n for n in got.notes))
        # the governing approval's percent cannot be read -> 0 %, not the older one
        approvals = [approval("5", date="2026-04-01T10:00:00Z"), approval("about ten")]
        self.assertEqual(plan(findings(approvals=approvals)).discount_percent, D(0))


class Robustness(unittest.TestCase):
    def test_empty_findings(self):
        got = build_plan(Findings(), REF, DAY, DOMAIN)
        self.assertEqual((got.decision, got.entries, got.total_net), ("bid", [], D("0.00")))

    def test_missing_parts_omit(self):
        for found, word in ((findings(requirements=[]), "not found in the specification"),
                            (findings(erps=[]), "no ERP lookup"),
                            (findings(erps=[erp(candidates=[])]), "no ERP candidates"),
                            (findings(positions=[tpos(unit="Palette")]), "unknown unit")):
            e = entry(found)
            self.assertTrue(e.omit)
            self.assertIn(word, e.reason)


if __name__ == "__main__":
    unittest.main()
