"""Bid-rule calculator on invented facts; expected values are worked out by hand in the comments."""
import sys
import unittest
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calc import (  # noqa: E402
    Amendment,
    Approval,
    BidInput,
    BonusBand,
    CalcError,
    CentAllocation,
    DatedRecord,
    DeliveryCommitment,
    DiscountScope,
    Material,
    MetalIndexRate,
    PackagingTariff,
    PositionInput,
    PriceCondition,
    PriceScale,
    ProjectBonus,
    QuantityChange,
    SetupPool,
    TieOrder,
    Unit,
    allocate_cents,
    apply_amendments,
    bonus_credit,
    calculate,
    latest_authoritative,
    matching_materials,
    metal_rate,
    nominal_stroke,
    parse_unit,
    position_key,
    reached_scale,
    resolve_quantity,
    to_pieces,
    valid_condition,
)

DAY = date(2026, 5, 15)


def cond(*scales: tuple[int, str], currency="EUR", fx_key=None, price_unit=1, start=date(2026, 1, 1), end=None):
    return PriceCondition(valid_from=start, valid_to=end, currency=currency, fx_key=fx_key, price_unit=price_unit,
                          scales=[PriceScale(min_quantity=q, price=D(p)) for q, p in scales])


def mat(number="M-100", stock=0, stock_lt=2, prod_lt=8, carton=None, alloc="0", mass="0", index=None):
    return Material(material_number=number, free_stock=stock, stock_lead_time_weeks=stock_lt,
                    production_lead_time_weeks=prod_lt, pieces_per_carton=carton, allocation_mass=D(alloc),
                    surcharge_mass=D(mass), metal_index=index)


def pos(position, quantity, price="10.00", material=None, **kw):
    kw.setdefault("conditions", [cond((1, price))])
    return PositionInput(position=position, quantity=quantity, material=material or mat(), **kw)


def bid(*positions, **kw):
    return BidInput(tender_reference="T-0001", scenario_date=DAY, positions=list(positions), **kw)


def by_pos(result):
    return {p.position: p for p in result.positions}


class Quantity(unittest.TestCase):
    """Rules 1, 13, 17."""

    def test_latest_remaining_change_wins(self):
        changes = [QuantityChange(number="3", date=datetime(2026, 2, 1), position="1", quantity=12),
                   QuantityChange(number="7", date=datetime(2026, 3, 1), position="1", quantity=15),
                   QuantityChange(number="9", date=datetime(2026, 4, 1), position="2", quantity=99)]
        # Q7 is the latest for position 1 -> 15
        self.assertEqual(resolve_quantity(10, "1", changes, set(), DAY), 15)
        # Q7 withdrawn -> Q3 remains -> 12
        self.assertEqual(resolve_quantity(10, "1", changes, {"7"}, DAY), 12)
        # both withdrawn -> tender quantity 10
        self.assertEqual(resolve_quantity(10, "1", changes, {"3", "7"}, DAY), 10)

    def test_future_change_ignored(self):
        changes = [QuantityChange(number="4", date=datetime(2026, 6, 1), position="1", quantity=40)]
        self.assertEqual(resolve_quantity(10, "1", changes, set(), DAY), 10)

    def test_sets_to_pieces(self):
        # 4 Satz x 6 pieces per set = 24 pieces
        self.assertEqual(to_pieces(4, parse_unit("Satz"), 6), 24)
        self.assertEqual(to_pieces(4, parse_unit("Stück"), None), 4)
        self.assertEqual(parse_unit("Sätze"), Unit.SET)
        with self.assertRaises(CalcError):
            to_pieces(4, Unit.SET, None)

    def test_set_quantity_in_bid(self):
        # 3 Satz x 4 = 12 pieces; scale from 10 pieces applies (9.00), bid quantity is 12 pieces
        p = pos("1", 3, unit=Unit.SET, pieces_per_set=4, conditions=[cond((1, "10.00"), (10, "9.00"))])
        out = by_pos(calculate(bid(p)))["1"]
        self.assertEqual((out.quantity, out.unit_price), (12, D("9.00")))


class Requirement(unittest.TestCase):
    """Rules 3, 14, 20."""

    def test_scoped_amendments_latest_per_attribute(self):
        base = {"bore": D(50), "stroke": D(200), "mounting": "Flansch"}
        amendments = [
            Amendment(date=datetime(2026, 2, 1), revision="B", requirement="R1", attributes={"stroke": D(210)}),
            Amendment(date=datetime(2026, 3, 1), revision="B", requirement="R1", attributes={"stroke": D(220)}),
            Amendment(date=datetime(2026, 4, 1), revision="A", requirement="R1", attributes={"bore": D(63)}),
            Amendment(date=datetime(2026, 4, 1), revision="B", requirement="R2", attributes={"bore": D(80)}),
            Amendment(date=datetime(2026, 9, 1), revision="B", requirement="R1", attributes={"stroke": D(999)}),
        ]
        got = apply_amendments(base, amendments, "B", "R1", DAY)
        # only revision B / R1, not future: stroke 220, rest inherited
        self.assertEqual(got, {"bore": D(50), "stroke": D(220), "mounting": "Flansch"})

    def test_nominal_stroke(self):
        # usable 220 + 2 x 15 reserve = 250
        self.assertEqual(nominal_stroke(D(220), D(15)), D(250))

    def test_exact_match(self):
        req = {"bore": D(50), "stroke": D(250), "pressure": D(160), "mounting": "Flansch", "certification": "X1"}
        candidates = [
            ("M-100", {"bore": "50.0", "stroke": "250", "pressure": "160", "mounting": "flansch",
                       "certification": "X1"}),
            ("M-101", {"bore": "50", "stroke": "240", "pressure": "160", "mounting": "Flansch", "certification": "X1"}),
            ("M-102", {"bore": "50", "stroke": "250", "pressure": "160", "mounting": "Gabel", "certification": "X1"}),
            ("M-103", {"bore": "50", "stroke": "250", "pressure": "160", "mounting": "Flansch"}),
        ]
        self.assertEqual(matching_materials(req, candidates), ["M-100"])

    def test_missing_material_is_no_bid(self):
        # rules 3, 8, 16: no positions, total 0, conditional approval -> 0 %
        p2 = PositionInput(position="2", quantity=5, material=None)
        out = calculate(bid(pos("1", 10), p2, approval=Approval(percent=D(5), min_order_value=D(1))))
        self.assertEqual((out.decision, out.positions, out.total_net, out.discount_percent),
                         ("no_bid", [], D("0.00"), D(0)))
        self.assertEqual(out.missing_materials, ["2"])
        self.assertEqual(out.answer()["positions"], [])

    def test_no_bid_unconditional_discount_kept(self):
        out = calculate(bid(PositionInput(position="1", quantity=5), approval=Approval(percent=D(7))))
        self.assertEqual(out.discount_percent, D(7))


class Pricing(unittest.TestCase):
    """Rules 4, 5, 7, 22, 27."""

    def test_condition_valid_on_date(self):
        old = cond((1, "8.00"), start=date(2025, 1, 1), end=date(2025, 12, 31))
        new = cond((1, "9.00"), start=date(2026, 1, 1), end=date(2026, 5, 15))  # inclusive end
        later = cond((1, "11.00"), start=date(2026, 5, 16))
        self.assertEqual(valid_condition([old, new, later], DAY).scales[0].price, D("9.00"))
        with self.assertRaises(CalcError):
            valid_condition([old], DAY)

    def test_highest_scale_reached(self):
        scales = cond((100, "8.50"), (1, "10.00"), (50, "9.00")).scales
        self.assertEqual(reached_scale(scales, 99).price, D("9.00"))
        self.assertEqual(reached_scale(scales, 100).price, D("8.50"))
        with self.assertRaises(CalcError):
            reached_scale(scales, 0)

    def test_discounted_unit_price_and_total(self):
        # 20 pieces reach scale 10 -> 9.50; 5 % -> 9.025 -> 9.03; total 20 x 9.03 = 180.60
        p = pos("1", 20, conditions=[cond((1, "10.00"), (10, "9.50"))])
        out = calculate(bid(p, approval=Approval(percent=D(5))))
        line = by_pos(out)["1"]
        self.assertEqual((line.base_unit_price, line.unit_price, out.total_net), (D("9.50"), D("9.03"), D("180.60")))
        self.assertEqual(out.discount_percent, D(5))

    def test_fx_and_price_unit(self):
        # 1234.56 per 100 pieces = 12.3456 per piece x 0.9 EUR/USD = 11.11104 -> 11.11
        c = cond((1, "1234.56"), currency="USD", fx_key="FX-1", price_unit=100)
        out = calculate(bid(pos("1", 10, conditions=[c]), fx_rates={"FX-1": D("0.9")}))
        line = by_pos(out)["1"]
        self.assertEqual((line.base_unit_price, line.unit_price, out.total_net), (D("11.11"), D("11.11"), D("111.10")))

    def test_missing_fx_rate(self):
        with self.assertRaises(CalcError):
            calculate(bid(pos("1", 10, conditions=[cond((1, "5"), currency="USD", fx_key="FX-1")])))

    def test_round_once_from_unrounded_components(self):
        # base 1.00; surcharge 0.1 kg x 0.04 = 0.004; packaging 1 carton x 0.04 / 10 = 0.004
        # exact 1.008 -> 1.01 (rounding the components first would give 1.00); total 10 x 1.01 = 10.10
        m = mat(carton=10, mass="0.1", index="IDX")
        p = pos("1", 10, price="1.00", material=m, packaging=PackagingTariff(price_per_carton=D("0.04")))
        out = calculate(bid(p, metal_indices={"IDX": [MetalIndexRate(rate=D("0.04"))]}))
        line = by_pos(out)["1"]
        self.assertEqual((line.surcharge_per_unit, line.packaging_per_unit), (D("0.00"), D("0.00")))
        self.assertEqual((line.unit_price, out.total_net), (D("1.01"), D("10.10")))

    def test_answer_shape(self):
        out = calculate(bid(pos("1", 3, price="2.50")))
        ans = out.answer()
        self.assertEqual(set(ans), {"tender_reference", "decision", "currency", "discount_percent", "positions",
                                    "total_net"})
        self.assertEqual(ans["positions"][0], {"position": "1", "material_number": "M-100", "quantity": 3,
                                               "unit_price": 2.5, "lead_time_weeks": 8, "base_unit_price": 2.5,
                                               "surcharge_per_unit": 0.0, "packaging_per_unit": 0.0,
                                               "rebate_total": 0.0})
        self.assertEqual(ans["total_net"], 7.5)


class Discount(unittest.TestCase):
    """Rules 5, 12, 16."""

    def two(self, approval):
        # A: 10 x 10.00 = 100.00 plus 0.5 kg x 4.00 = 2.00 surcharge; B: 5 x 20.00 = 100.00; order value 200.00
        a = pos("A", 10, material=mat(mass="0.5", index="IDX"))
        b = pos("B", 5, price="20.00", material=mat("M-200"))
        return calculate(bid(a, b, approval=approval, metal_indices={"IDX": [MetalIndexRate(rate=D("4.00"))]}))

    def test_threshold_inclusive(self):
        out = self.two(Approval(percent=D(10), min_order_value=D("200.00")))
        # surcharge excluded from threshold; 200.00 >= 200.00 -> 10 %: A 9.00 + 2.00 = 11.00, B 18.00
        self.assertEqual(out.order_value, D("200.00"))
        self.assertEqual(out.discount_percent, D(10))
        self.assertEqual({k: v.unit_price for k, v in by_pos(out).items()}, {"A": D("11.00"), "B": D("18.00")})
        # total 10 x 11.00 + 5 x 18.00 = 200.00
        self.assertEqual(out.total_net, D("200.00"))

    def test_threshold_failed_is_zero(self):
        # 200.00 < 200.01 -> 0 % even though surcharges would lift the value above it
        out = self.two(Approval(percent=D(10), min_order_value=D("200.01")))
        self.assertEqual(out.discount_percent, D(0))
        self.assertEqual(by_pos(out)["A"].unit_price, D("12.00"))

    def test_konditionsgruppen(self):
        # base 10.00, surcharge 0.5 kg x 4.00 = 2.00, discount 10 %
        # base only 9.00 + 2.00 = 11.00; base + surcharge 9.00 + 1.80 = 10.80; none 12.00; reported surcharge 2.00
        m = mat(mass="0.5", index="IDX")
        positions = [pos("1", 10, material=m),
                     pos("2", 10, material=m, discount_scope=DiscountScope.BASE_AND_SURCHARGE),
                     pos("3", 10, material=m, discount_scope=DiscountScope.NONE)]
        out = by_pos(calculate(bid(*positions, approval=Approval(percent=D(10)),
                                   metal_indices={"IDX": [MetalIndexRate(rate=D("4.00"))]})))
        self.assertEqual([out[k].unit_price for k in "123"], [D("11.00"), D("10.80"), D("12.00")])
        self.assertEqual({out[k].surcharge_per_unit for k in "123"}, {D("2.00")})

    def test_latest_authoritative_record(self):
        r = "finanzen@example.com"
        recs = [
            DatedRecord(reference="T-0001", sender=r, date=datetime(2026, 3, 1), key="old"),
            DatedRecord(reference="T-0001", sender=r, date=datetime(2026, 4, 1), key="new"),
            DatedRecord(reference="T-0001-A", sender=r, date=datetime(2026, 5, 1), key="lookalike"),
            DatedRecord(reference="T-0001", sender="finanzen@example.org", date=datetime(2026, 5, 2), key="spoof"),
            DatedRecord(reference="T-0001", sender=r, date=datetime(2026, 6, 1), key="future"),
        ]
        self.assertEqual(latest_authoritative(recs, "T-0001", DAY, "finanzen", "example.com").key, "new")
        self.assertIsNone(latest_authoritative(recs, "T-0002", DAY, "finanzen", "example.com"))


class Delivery(unittest.TestCase):
    """Rules 6, 15, 24."""

    def run_two(self, stock, c1=None, c2=None):
        # positions 20 + 15 pieces of M-100, pooled demand 35; scales per position: 20 < 30, so 10.00 for both
        m = mat(stock=stock)
        conds = [cond((1, "10.00"), (30, "9.00"))]
        out = calculate(bid(pos("1", 20, material=m, conditions=conds, commitment=c1),
                            pos("2", 15, material=m, conditions=conds, commitment=c2)))
        return by_pos(out)

    def test_pooled_stock(self):
        short = self.run_two(stock=30)  # 30 < 35 -> production for both
        self.assertEqual([short[k].lead_time_weeks for k in "12"], [8, 8])
        self.assertEqual([short[k].unit_price for k in "12"], [D("10.00"), D("10.00")])
        enough = self.run_two(stock=35)  # 35 >= 35 -> stock for both
        self.assertEqual([enough[k].lead_time_weeks for k in "12"], [2, 2])

    def test_commitment_per_position(self):
        c_in = DeliveryCommitment(material_number="M-100", ceiling=35, lead_time_weeks=4)  # 35 <= 35 applies
        c_low = DeliveryCommitment(material_number="M-100", ceiling=34, lead_time_weeks=3)  # 35 > 34 -> fallback
        got = self.run_two(stock=30, c1=c_in, c2=c_low)
        self.assertEqual([got[k].lead_time_weeks for k in "12"], [4, 8])
        other = DeliveryCommitment(material_number="M-999", ceiling=100, lead_time_weeks=1)
        got = self.run_two(stock=35, c1=other)
        self.assertEqual(got["1"].lead_time_weeks, 2)


class MetalIndex(unittest.TestCase):
    """Rule 28."""

    RATES = [MetalIndexRate(rate=D("3.00"), valid_from=date(2026, 1, 1), valid_to=date(2026, 3, 31)),
             MetalIndexRate(rate=D("4.00"), valid_from=date(2026, 4, 1), valid_to=date(2026, 6, 30)),
             MetalIndexRate(rate=D("5.00"), valid_from=date(2026, 7, 1), valid_to=date(2026, 12, 31))]

    def test_window_contains_date(self):
        self.assertEqual(metal_rate(self.RATES, DAY), D("4.00"))  # not the last listed 5.00
        self.assertEqual(metal_rate(self.RATES, date(2026, 6, 30)), D("4.00"))  # inclusive end
        with self.assertRaises(CalcError):
            metal_rate(self.RATES, date(2027, 1, 1))

    def test_surcharge_in_bid(self):
        # 0.5 kg x 4.00 = 2.00 per piece; unit 10.00 + 2.00 = 12.00
        out = by_pos(calculate(bid(pos("1", 10, material=mat(mass="0.5", index="IDX")),
                                   metal_indices={"IDX": self.RATES})))["1"]
        self.assertEqual((out.surcharge_per_unit, out.unit_price), (D("2.00"), D("12.00")))


class Packaging(unittest.TestCase):
    """Rule 23."""

    def test_cartons_rounded_up_not_discounted(self):
        # 25 pieces / 10 per carton -> 3 cartons x 1.50 = 4.50 -> 0.18 per piece
        # unit = 10.00 x 0.9 + 4.50 / 25 = 9.18; quantity stays 25
        p = pos("1", 25, material=mat(carton=10), packaging=PackagingTariff(price_per_carton=D("1.50")))
        out = by_pos(calculate(bid(p, approval=Approval(percent=D(10)))))["1"]
        self.assertEqual((out.quantity, out.packaging_per_unit, out.unit_price), (25, D("0.18"), D("9.18")))

    def test_packaging_rounding(self):
        # 7 pieces in one carton at 1.00 -> 0.142857 -> 0.14; unit 10.142857 -> 10.14
        p = pos("1", 7, material=mat(carton=10), packaging=PackagingTariff(price_per_carton=D("1.00")))
        out = by_pos(calculate(bid(p)))["1"]
        self.assertEqual((out.packaging_per_unit, out.unit_price), (D("0.14"), D("10.14")))

    def test_unknown_carton_size(self):
        with self.assertRaises(CalcError):
            calculate(bid(pos("1", 7, packaging=PackagingTariff(price_per_carton=D("1.00")))))


class SetupFees(unittest.TestCase):
    """Rule 25."""

    def test_largest_remainder_ties_ascending(self):
        # 100.00 over three equal weights: 3333.33 cents each -> 3333 x 3, 1 cent left -> first by position
        w = {"3": D(10), "1": D(10), "2": D(10)}
        self.assertEqual(allocate_cents(D("100.00"), w), {"1": D("33.34"), "2": D("33.33"), "3": D("33.33")})
        desc = allocate_cents(D("100.00"), w, tie_order=TieOrder.DESCENDING)
        self.assertEqual(desc["3"], D("33.34"))

    def test_natural_position_order(self):
        # 0.01 over two equal weights: tie goes to "2" before "10"
        self.assertEqual(allocate_cents(D("0.01"), {"10": D(1), "2": D(1)}), {"2": D("0.01"), "10": D("0.00")})
        self.assertLess(position_key("2"), position_key("10"))

    def test_variants(self):
        w = {"A": D(20), "B": D(20)}  # 10.01 -> 500.5 cents each
        self.assertEqual(allocate_cents(D("10.01"), w, CentAllocation.ROUND_EACH), {"A": D("5.01"), "B": D("5.01")})
        self.assertEqual(allocate_cents(D("10.01"), w, CentAllocation.REST_TO_LAST), {"A": D("5.01"), "B": D("5.00")})
        self.assertEqual(allocate_cents(D("10.01"), w, CentAllocation.REST_TO_FIRST, rest_base="floor"),
                         {"A": D("5.01"), "B": D("5.00")})
        # weights 30 vs 10 of 1.00 -> 75.0 / 25.0 cents, nothing to fix
        self.assertEqual(allocate_cents(D("1.00"), {"A": D(30), "B": D(10)}, CentAllocation.REST_TO_LARGEST),
                         {"A": D("0.75"), "B": D("0.25")})

    def test_pool_in_bid(self):
        # pool 100.00 over 1, 2, 3 by pieces x allocation mass (10 x 1 each) -> 33.34 / 33.33 / 33.33
        # pos 1: setup 20.00 + 33.34 = 53.34 over 10 pieces = 5.334 -> surcharge 5.33
        # unit = 10.00 x 0.9 + 5.334 = 14.334 -> 14.33 (setup never discounted)
        m = mat(alloc="1")
        positions = [pos("1", 10, material=m, setup_fee=D("20.00")), pos("2", 10, material=m),
                     pos("3", 10, material=m), pos("4", 10, material=m)]
        out = by_pos(calculate(bid(*positions, approval=Approval(percent=D(10)),
                                   setup_pool=SetupPool(fee=D("100.00"), members=["1", "2", "3"]))))
        self.assertEqual((out["1"].surcharge_per_unit, out["1"].unit_price), (D("5.33"), D("14.33")))
        # pos 2: 9.00 + 3.333 = 12.333 -> 12.33; pos 4 not a member: 9.00, surcharge 0.00
        self.assertEqual((out["2"].unit_price, out["4"].unit_price, out["4"].surcharge_per_unit),
                         (D("12.33"), D("9.00"), D("0.00")))

    def test_allocation_mass_not_surcharge_mass(self):
        # A: 10 pcs x alloc 2 = 20, B: 20 pcs x alloc 1 = 20 -> equal halves of 10.01 -> A 5.01, B 5.00
        a = pos("A", 10, material=mat("M-1", alloc="2", mass="0"))
        b = pos("B", 20, material=mat("M-2", alloc="1", mass="0"))
        out = calculate(bid(a, b, setup_pool=SetupPool(fee=D("10.01"), members=["A", "B"])))
        self.assertEqual({d.position: d.pool_allocation for d in out.debug}, {"A": D("5.01"), "B": D("5.00")})


class Bonus(unittest.TestCase):
    """Rule 26."""

    BANDS = [BonusBand(lower=D(0), upper=D(1000), percent=D(0)), BonusBand(lower=D(1000), upper=D(5000), percent=D(2)),
             BonusBand(lower=D(5000), percent=D(3))]

    def test_marginal_bands(self):
        # 6000: 4000 x 2 % + 1000 x 3 % = 80 + 30 = 110.00
        self.assertEqual(bonus_credit(D("6000.00"), self.BANDS), D("110.00"))
        self.assertEqual(bonus_credit(D("999.99"), self.BANDS), D("0.00"))
        # 1234.56 x 1.5 % = 18.5184 -> 18.52
        self.assertEqual(bonus_credit(D("1234.56"), [BonusBand(lower=D(0), percent=D("1.5"))]), D("18.52"))

    def test_split_and_unit_price(self):
        # members 1-3: 100 x 20.00 = 2000.00 each, total 6000.00 -> credit 110.00 = 11000 cents
        # 3666.67 each -> floor 3666 x 3, 2 cents left, equal remainders -> positions 1, 2: 36.67, 36.67, 36.66
        # unit pos 1: 20.00 - 36.67 / 100 = 19.6333 -> 19.63; pos 4 non-member: rebate 0.00, unit 20.00
        positions = [pos(k, 100, price="20.00") for k in "1234"]
        out = calculate(bid(*positions, approval=Approval(percent=D(50)),
                            project_bonus=ProjectBonus(bands=self.BANDS, members=["1", "2", "3"])))
        lines = by_pos(out)
        self.assertEqual(out.bonus_total, D("110.00"))
        self.assertEqual([lines[k].rebate_total for k in "1234"], [D("36.67"), D("36.67"), D("36.66"), D("0.00")])
        # bonus uses undiscounted goods although 50 % applies: unit = 10.00 - 0.3667 = 9.6333 -> 9.63
        self.assertEqual((lines["1"].unit_price, lines["4"].unit_price), (D("9.63"), D("10.00")))


if __name__ == "__main__":
    unittest.main()
