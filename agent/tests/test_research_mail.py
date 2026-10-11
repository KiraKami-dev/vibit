"""Mail selection without the mailbox address, and the per-kind time budget (no browser, no model)."""
import sys
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calc  # noqa: E402
import research_mail as rm  # noqa: E402


def record(sender: str, recipient: str, fee: str, day: int) -> rm.MailRecord:
    return rm.MailRecord[rm.PackagingContent](
        subject="Tariffs T-1", sender=sender, recipient=recipient, sent_at=datetime(2030, 1, day, tzinfo=UTC),
        reference="T-1", is_record=True, content=rm.PackagingContent(entries=[rm.Tariff(key="P-1",
                                                                                         price_per_carton=fee)]))


KIND = rm.Kind(name="packaging", subject="Tariffs", sender="logistics", sender_form="local_part_at_bidder_domain",
               site="mail", what="tariffs", schema=rm.SCHEMAS["packaging"])


class MailDomainTest(unittest.TestCase):
    def test_records_are_authorised_by_the_recipients_domain_without_an_address(self):
        own = "sales@example.com"
        result = rm.KindResult(KIND, records=[
            record("logistics@example.com", own, "1.50", 2),
            record("logistics@example.org", own, "9.99", 3),  # look-alike domain, newer
            record("logistics@example.com", "sales@example.com", "2.50", 4),
        ])
        facts = rm.resolve_mail([result], "T-1", "", datetime(2030, 2, 1, tzinfo=UTC), "")
        self.assertEqual(str(facts.packaging["P-1"].price_per_carton), "2.50")
        self.assertTrue(any("recipients" in p for p in facts.problems))

    def test_the_address_wins_over_the_recipients(self):
        self.assertEqual(rm.bidder_domain("sales@example.net", [record("a@example.com", "b@example.com", "1", 2)]),
                         "example.net")
        self.assertEqual(rm.bidder_domain("", []), "")


class KindBudgetTest(unittest.TestCase):
    def test_budget_shrinks_near_the_deadline(self):
        self.assertEqual(rm.kind_budget(0.0, None), rm.KIND_SECONDS)
        self.assertEqual(rm.kind_budget(0.0, 1000.0), rm.KIND_SECONDS)
        self.assertEqual(rm.kind_budget(0.0, 200.0 + rm.READ_RESERVE), 100.0)
        self.assertEqual(rm.kind_budget(0.0, 30.0 + rm.READ_RESERVE), 30.0)
        self.assertEqual(rm.kind_budget(0.0, 10.0), 0.0)

    def test_approval_is_planned_first(self):
        kinds = [rm.Kind(name=n, subject=n, sender="x", sender_form="name", site="mail", what=n,
                         schema=rm.SCHEMAS[n]) for n in ("fx", "packaging", "approval")]
        self.assertEqual([k.name for k in sorted(kinds, key=lambda k: k.name not in rm.FIRST)],
                         ["approval", "fx", "packaging"])


def kind(name: str, sender="finance", form="local_part_at_bidder_domain") -> rm.Kind:
    return rm.Kind(name=name, subject=name, sender=sender, sender_form=form, site="mail", what=name,
                   schema=rm.SCHEMAS[name])


def message(content, sender="finance@example.com", name="", fmt="de", day=2) -> rm.MailRecord:
    return rm.MailRecord[type(content)](
        subject="Record T-1", sender=sender, sender_name=name, number_format=fmt,
        sent_at=datetime(2030, 1, day, tzinfo=UTC), reference="T-1", is_record=True, content=content)


SCENARIO = datetime(2030, 2, 1, tzinfo=UTC)


class ConversionTest(unittest.TestCase):
    """The reader reports rates as written, with their direction and mass unit; the code converts (rules 22, 28)."""

    def resolve(self, name, content):
        return rm.resolve_mail([rm.KindResult(kind(name), records=[message(content)])], "T-1", "example.com",
                               SCENARIO, "sales@example.com")

    def test_fx_in_both_directions(self):
        facts = self.resolve("fx", rm.FxContent(entries=[
            rm.FxRate(key="FX-A", currency="USD", rate="1,08", direction="currency_per_eur"),
            rm.FxRate(key="FX-B", currency="USD", rate="0,93", direction="eur_per_currency"),
            rm.FxRate(key="FX-C", currency="JPY", rate="0,62", direction="eur_per_currency", amount="100")]))
        self.assertEqual(facts.fx_per_eur, {"FX-A": Decimal("1.08")})
        self.assertEqual(facts.fx_rates, {"FX-B": Decimal("0.93"), "FX-C": Decimal("0.0062")})

    def test_fx_decimal_point_in_a_de_message(self):
        # the message is "de" (as most of its figures) but quotes the rate with a decimal point: 1.2345, not 12345
        facts = self.resolve("fx", rm.FxContent(entries=[
            rm.FxRate(key="FX-D", currency="USD", rate="1.2345", direction="eur_per_currency")]))
        self.assertEqual(facts.fx_rates, {"FX-D": Decimal("1.2345")})
        clauses = rm.bid_clauses(SimpleNamespace(positions=[]), facts)
        condition = calc.PriceCondition(valid_from=datetime(2030, 1, 1, tzinfo=UTC), currency="USD", fx_key="FX-D",
                                        price_unit=100, scales=[calc.PriceScale(min_quantity=1,
                                                                                price=Decimal("60000"))])
        material = calc.Material(material_number="M-1", stock_lead_time_weeks=1, production_lead_time_weeks=2)
        result = calc.calculate(calc.BidInput(
            tender_reference="T-1", scenario_date=SCENARIO, fx_rates=clauses.fx_rates, fx_per_eur=clauses.fx_per_eur,
            positions=[calc.PositionInput(position="1", quantity=1, material=material, conditions=[condition])]))
        self.assertEqual(result.positions[0].base_unit_price, Decimal("740.70"))  # 600.00 x 1.2345
        self.assertEqual(calc.implausible_prices(result), {})

    def test_metal_index_per_tonne_becomes_per_kg(self):
        facts = self.resolve("metal_indices", rm.MetalIndexContent(rates=[
            rm.IndexRate(code="IDX-1", rate="4.250,00", per="tonne"),
            rm.IndexRate(code="IDX-2", rate="3,10")]))
        self.assertEqual(facts.metal_indices["IDX-1"][0].rate, Decimal("4.25"))
        self.assertEqual(facts.metal_indices["IDX-2"][0].rate, Decimal("3.10"))

    def test_fx_reaches_calc_divided(self):
        facts = self.resolve("fx", rm.FxContent(entries=[
            rm.FxRate(key="FX-A", currency="USD", rate="1,25", direction="currency_per_eur")]))
        clauses = rm.bid_clauses(SimpleNamespace(positions=[]), facts)
        condition = calc.PriceCondition(valid_from=datetime(2030, 1, 1, tzinfo=UTC), currency="USD", fx_key="FX-A",
                                        scales=[calc.PriceScale(min_quantity=1, price=Decimal("10.00"))])
        material = calc.Material(material_number="M-1", stock_lead_time_weeks=1, production_lead_time_weeks=2)
        result = calc.calculate(calc.BidInput(
            tender_reference="T-1", scenario_date=SCENARIO, fx_rates=clauses.fx_rates, fx_per_eur=clauses.fx_per_eur,
            positions=[calc.PositionInput(position="1", quantity=1, material=material, conditions=[condition])]))
        self.assertEqual(result.positions[0].base_unit_price, Decimal("8.00"))  # 10.00 / 1.25


class SenderTest(unittest.TestCase):
    """Rule 21: the right sender, by address or name, and the bidder's mailbox domain."""

    def test_name_form_matches_display_name_or_local_part_on_the_bidder_domain(self):
        k = kind("approval", sender="Sales Director", form="name")
        content = rm.ApprovalContent(percent="5")
        self.assertTrue(rm.sender_ok(message(content, "a.person@example.com", "Sales Director"), k, "example.com"))
        self.assertTrue(rm.sender_ok(message(content, "sales.director@example.com"), k, "example.com"))
        self.assertTrue(rm.sender_ok(message(content, "Sales Director <sales-director@example.com>"), k,
                                     "example.com"))
        # the same display name from another domain is a look-alike
        self.assertFalse(rm.sender_ok(message(content, "a.person@example.org", "Sales Director"), k, "example.com"))
        # another person on the right domain
        self.assertFalse(rm.sender_ok(message(content, "sales@example.com", "Sales Team"), k, "example.com"))
        # domain unknown: the name decides
        self.assertTrue(rm.sender_ok(message(content, "a.person@example.org", "Sales Director"), k, ""))

    def test_local_part_form_reads_the_address_out_of_a_name(self):
        k = kind("packaging", sender="logistics")
        content = rm.PackagingContent(entries=[])
        self.assertTrue(rm.sender_ok(message(content, "Logistics <logistics@example.com>"), k, "example.com"))
        self.assertFalse(rm.sender_ok(message(content, "logistics@example.org"), k, "example.com"))


class SeveralKeysTest(unittest.TestCase):
    """Rules 25 and 26: every pool and plan key the Q&A assigns becomes its own calc input."""

    def test_pools_and_plans_per_key(self):
        def p(number, pool=None, plan=None):
            return SimpleNamespace(position=number, quantity=1, unit=calc.Unit.PIECE, setup_fee=Decimal(0),
                                   condition_group=None, packaging_key=None, commitment_key=None,
                                   setup_pool_key=pool, bonus_key=plan)
        tender = SimpleNamespace(positions=[p("1", "P-A", "B-X"), p("2", "P-A"), p("3", "P-B", "B-Y"),
                                            p("4", "P-C", "B-Z")])
        terms = rm.PoolTerms(fee=Decimal("9.00"), allocation=calc.CentAllocation.LARGEST_REMAINDER,
                             tie_order=calc.TieOrder.ASCENDING, rest_base="half_up")
        bonus = rm.BonusTerms(bands=[calc.BonusBand(lower=Decimal(0), percent=Decimal(1))], include_surcharge=False)
        mail = rm.MailFacts(mailbox="sales@example.com", setup_pools={"P-A": terms, "P-B": terms},
                            project_bonuses={"B-X": bonus, "B-Y": bonus})
        clauses = rm.bid_clauses(tender, mail)
        self.assertEqual([(x.key, x.members) for x in clauses.setup_pools], [("P-A", ["1", "2"]), ("P-B", ["3"])])
        self.assertEqual([(x.key, x.members) for x in clauses.project_bonuses], [("B-X", ["1"]), ("B-Y", ["3"])])
        self.assertTrue(any("P-C" in problem for problem in clauses.problems))
        self.assertTrue(any("B-Z" in problem for problem in clauses.problems))


class LandedTest(unittest.TestCase):
    def test_a_sign_in_redirect_is_not_the_message(self):
        want = "https://mail.example.com/?folder=inbox&message=m-1"
        self.assertTrue(rm._landed(want, want))
        self.assertFalse(rm._landed("https://mail.example.com/login?returnTo=%2F", want))
        self.assertFalse(rm._landed("https://mail.example.com/?folder=inbox", want))


if __name__ == "__main__":
    unittest.main()
