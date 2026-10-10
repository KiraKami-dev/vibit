"""
Deterministic bid rules: quantities, prices, discount, surcharges, packaging, setup pool, project bonus, lead times.

Pure and model-free. The workers establish the facts and the model picks which record applies (which mail, which
revision); calc only does what the bid rules define mechanically. Money is Decimal throughout and rounds HALF_UP
only at the steps the rules name.
"""
import math
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

CENT = Decimal("0.01")
HUNDRED = Decimal(100)


class CalcError(ValueError):
    """The facts are incomplete or contradictory, so a field cannot be computed."""


def money(value: Decimal) -> Decimal:
    """Round HALF_UP to cents."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def position_key(position: str) -> tuple:
    """Natural sort key for position identifiers, so "2" sorts before "10" and "1.2" before "1.10"."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.findall(r"\d+|\D+", position))


def _day(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


# ---------------------------------------------------------------- input models


class Unit(StrEnum):
    PIECE = "piece"
    SET = "set"


def parse_unit(text: str) -> Unit:
    """Map a stated unit (Stück, St, ST, pcs, Satz, Sätze, Set) to a Unit."""
    t = text.strip().lower().rstrip(".")
    if t.startswith(("satz", "sätz", "saetz", "set")):
        return Unit.SET
    if t in {"st", "stk", "stck", "stück", "stueck", "pc", "pcs", "piece", "pieces"}:
        return Unit.PIECE
    raise CalcError(f"unknown unit {text!r}")


class DiscountScope(StrEnum):
    """What the order-level discount applies to for one position (rule 12; setup is never discounted)."""
    BASE_ONLY = "base_only"
    BASE_AND_SURCHARGE = "base_and_surcharge"
    NONE = "none"


class PriceScale(BaseModel):
    min_quantity: int = Field(ge=0, description="Scale reached from this many final pieces")
    price: Decimal = Field(description="Price per price unit, in the condition's currency")


class PriceCondition(BaseModel):
    """One ERP price condition record (rule 22). Price unit is the number of pieces the price refers to (1, 100)."""
    valid_from: date
    valid_to: date | None = None
    currency: str = "EUR"
    fx_key: str | None = None
    price_unit: int = Field(default=1, gt=0)
    scales: list[PriceScale]


class Material(BaseModel):
    """The chosen active ERP material with the attributes calc needs."""
    material_number: str
    free_stock: int = Field(default=0, description="Unrestricted free stock in pieces, not blocked stock")
    stock_lead_time_weeks: int
    production_lead_time_weeks: int
    pieces_per_carton: int | None = Field(default=None, gt=0)
    allocation_mass: Decimal = Field(default=Decimal(0), description="kg per piece, used for the setup pool")
    surcharge_mass: Decimal = Field(default=Decimal(0), description="kg per piece, used for the metal surcharge")
    metal_index: str | None = Field(default=None, description="Metal index code; None = the only index given")


class MetalIndexRate(BaseModel):
    """EUR per kg; a missing bound means the window is open on that side."""
    rate: Decimal
    valid_from: date | None = None
    valid_to: date | None = None


class PackagingTariff(BaseModel):
    price_per_carton: Decimal = Field(description="EUR per carton")


class DeliveryCommitment(BaseModel):
    """Terminfreigabe for one position (rule 24)."""
    material_number: str
    ceiling: int = Field(description="Inclusive maximum pooled demand in pieces for the material")
    lead_time_weeks: int


class CentAllocation(StrEnum):
    """
    How the setup pool fee is split into cents (rule 25: the rule is stated in the Ruestpools mail).

    LARGEST_REMAINDER: floor every exact share, hand out the missing cents by largest fractional part.
    ROUND_EACH: round every share HALF_UP, no correction (the sum may differ from the fee by a few cents).
    REST_TO_LARGEST / REST_TO_FIRST / REST_TO_LAST: round (or floor, see SetupPool.rest_base) every share, then the
    difference to the fee goes to the member with the largest weight / the first / the last member by tie order.
    """
    LARGEST_REMAINDER = "largest_remainder"
    ROUND_EACH = "round_each"
    REST_TO_LARGEST = "rest_to_largest"
    REST_TO_FIRST = "rest_to_first"
    REST_TO_LAST = "rest_to_last"


class TieOrder(StrEnum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


class SetupPool(BaseModel):
    """Shared setup (Ruestpool): one fee split over members by final pieces x allocation mass."""
    fee: Decimal
    members: list[str]
    allocation: CentAllocation = CentAllocation.LARGEST_REMAINDER
    tie_order: TieOrder = Field(default=TieOrder.ASCENDING, description="Position order for ties and first/last")
    rest_base: Literal["half_up", "floor"] = "half_up"


class BonusBand(BaseModel):
    """Marginal band: percent applies to the part of the eligible total between lower and upper (None = open)."""
    lower: Decimal
    upper: Decimal | None = None
    percent: Decimal


class ProjectBonus(BaseModel):
    bands: list[BonusBand]
    members: list[str]
    include_surcharge: bool = Field(default=False, description="Assumption: goods value = pieces x EUR base price; "
                                                               "True adds the undiscounted mass surcharge")


class Approval(BaseModel):
    """The governing director approval (rules 5, 16)."""
    percent: Decimal
    min_order_value: Decimal | None = Field(default=None, description="Inclusive threshold in EUR, if conditional")


class PositionInput(BaseModel):
    position: str
    quantity: int = Field(ge=0, description="Final quantity in the stated unit (after Q&A changes, rule 1/13)")
    unit: Unit = Unit.PIECE
    pieces_per_set: int | None = Field(default=None, gt=0, description="From the governing spec, for Satz")
    material: Material | None = Field(default=None, description="None = no exactly matching active material")
    conditions: list[PriceCondition] = Field(default_factory=list, description="Candidates; calc picks the valid one")
    discount_scope: DiscountScope = DiscountScope.BASE_ONLY
    setup_fee: Decimal = Field(default=Decimal(0), description="Ordinary setup fee for the whole position, EUR")
    packaging: PackagingTariff | None = None
    commitment: DeliveryCommitment | None = None


class BidInput(BaseModel):
    tender_reference: str
    scenario_date: date
    positions: list[PositionInput]
    approval: Approval | None = None
    fx_rates: dict[str, Decimal] = Field(default_factory=dict, description="FX key (or currency) -> EUR per unit")
    metal_indices: dict[str, list[MetalIndexRate]] = Field(default_factory=dict)
    setup_pool: SetupPool | None = None
    project_bonus: ProjectBonus | None = None


# ---------------------------------------------------------------- output models


class AnswerPosition(BaseModel):
    position: str
    material_number: str
    quantity: int
    unit_price: Decimal
    lead_time_weeks: int
    base_unit_price: Decimal | None = None
    surcharge_per_unit: Decimal | None = None
    packaging_per_unit: Decimal | None = None
    rebate_total: Decimal | None = None


class PositionDebug(BaseModel):
    """Unrounded intermediates of one position."""
    position: str
    pieces: int
    pooled_demand: int
    scale_min_quantity: int
    scale_price: Decimal
    currency: str
    fx_rate: Decimal
    base_eur: Decimal
    metal_rate: Decimal
    mass_surcharge: Decimal
    discount_factor_base: Decimal
    discount_factor_surcharge: Decimal
    setup_fee: Decimal
    pool_allocation: Decimal
    cartons: int
    carton_cost: Decimal
    bonus: Decimal
    unit_price_exact: Decimal


class BidResult(BaseModel):
    tender_reference: str
    decision: Literal["bid", "no_bid"]
    currency: Literal["EUR"] = "EUR"
    discount_percent: Decimal
    positions: list[AnswerPosition]
    total_net: Decimal
    order_value: Decimal | None = Field(default=None, description="Undiscounted value for the discount threshold")
    bonus_total: Decimal | None = None
    missing_materials: list[str] = Field(default_factory=list)
    debug: list[PositionDebug] = Field(default_factory=list)

    def answer(self, notes: str | None = None) -> dict:
        """The answer object per the answer schema, money as floats with two decimals."""
        out = {
            "tender_reference": self.tender_reference,
            "decision": self.decision,
            "currency": "EUR",
            "discount_percent": float(self.discount_percent),
            "positions": [{k: float(v) if isinstance(v, Decimal) else v
                           for k, v in p.model_dump().items() if v is not None} for p in self.positions],
            "total_net": float(self.total_net),
        }
        if notes:
            out["notes"] = notes[:4000]
        return out


# ---------------------------------------------------------------- mechanical selection helpers


class QuantityChange(BaseModel):
    number: str = Field(description="Q&A answer number")
    date: datetime
    position: str
    quantity: int


def resolve_quantity(tender_quantity: int, position: str, changes: list[QuantityChange], withdrawn: set[str],
                     scenario_date: date | None = None) -> int:
    """Rules 1 and 13: latest non-withdrawn change for the position (not future-dated), else the tender quantity."""
    live = [c for c in changes if c.position == position and c.number not in withdrawn
            and (scenario_date is None or _day(c.date) <= scenario_date)]
    if not live:
        return tender_quantity
    return max(live, key=lambda c: (c.date, position_key(c.number))).quantity


def to_pieces(quantity: int, unit: Unit, pieces_per_set: int | None) -> int:
    """Rule 17: Satz x pieces per set; pieces stay as they are."""
    if unit is Unit.PIECE:
        return quantity
    if not pieces_per_set:
        raise CalcError("quantity in sets but pieces per set unknown")
    return quantity * pieces_per_set


class Amendment(BaseModel):
    """A scoped technical amendment from the tender Q&A (rule 14)."""
    date: datetime
    revision: str
    requirement: str
    attributes: dict[str, Decimal | str]


def apply_amendments(attributes: dict[str, Decimal | str], amendments: list[Amendment], revision: str,
                     requirement: str, scenario_date: date | None = None) -> dict[str, Decimal | str]:
    """Rule 14: only amendments naming this revision and requirement; the latest dated one wins per attribute."""
    result = dict(attributes)
    latest: dict[str, datetime] = {}
    for a in amendments:
        if a.revision != revision or a.requirement != requirement:
            continue
        if scenario_date is not None and _day(a.date) > scenario_date:
            continue
        for key, value in a.attributes.items():
            if key not in latest or a.date >= latest[key]:
                latest[key] = a.date
                result[key] = value
    return result


def nominal_stroke(usable_stroke: Decimal, reserve_per_end: Decimal) -> Decimal:
    """Rule 20: nominal stroke = usable stroke (after amendments) + 2 x per-end reserve."""
    return usable_stroke + 2 * reserve_per_end


MATCH_KEYS = ("bore", "stroke", "pressure", "mounting", "certification")


def _same(a: Decimal | str, b: Decimal | str) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except ArithmeticError:
        return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def matching_materials(requirement: dict[str, Decimal | str], candidates: list[tuple[str, dict[str, Decimal | str]]],
                       keys: tuple[str, ...] = MATCH_KEYS) -> list[str]:
    """Rule 3: material numbers whose listed attributes all equal the requirement (numbers compared as numbers)."""
    return [number for number, attrs in candidates
            if all(k in attrs and k in requirement and _same(attrs[k], requirement[k]) for k in keys)]


class DatedRecord(BaseModel):
    """An authority record (mail) reduced to what rule 21 checks."""
    reference: str
    sender: str
    date: datetime
    key: str = ""


def latest_authoritative[R: DatedRecord](records: list[R], reference: str, scenario_date: date, sender_local: str,
                                         bidder_domain: str) -> R | None:
    """Rule 21: latest record for the exact reference from local@domain, dated no later than the scenario date."""
    sender = f"{sender_local}@{bidder_domain}".casefold()
    ok = [r for r in records if r.reference.strip() == reference.strip() and r.sender.strip().casefold() == sender
          and _day(r.date) <= scenario_date]
    return max(ok, key=lambda r: r.date) if ok else None


def valid_condition(conditions: list[PriceCondition], on: date) -> PriceCondition:
    """Rule 4: the condition whose inclusive validity contains the date (latest start if several)."""
    valid = [c for c in conditions if c.valid_from <= on and (c.valid_to is None or on <= c.valid_to)]
    if not valid:
        raise CalcError(f"no price condition valid on {on}")
    return max(valid, key=lambda c: c.valid_from)


def reached_scale(scales: list[PriceScale], pieces: int) -> PriceScale:
    """Rule 4: the highest scale whose minimum the pieces reach (per position, never the material sum)."""
    reached = [s for s in scales if s.min_quantity <= pieces]
    if not reached:
        raise CalcError(f"no price scale reached by {pieces} pieces")
    return max(reached, key=lambda s: s.min_quantity)


def metal_rate(rates: list[MetalIndexRate], on: date) -> Decimal:
    """Rule 28: the rate whose inclusive window contains the date; a single unwindowed rate applies as is."""
    hits = [r for r in rates
            if (r.valid_from is None or r.valid_from <= on) and (r.valid_to is None or on <= r.valid_to)]
    if not hits:
        raise CalcError(f"no metal index rate valid on {on}")
    windowed = [r for r in hits if r.valid_from is not None or r.valid_to is not None]
    return (windowed or hits)[-1].rate


def bonus_credit(total: Decimal, bands: list[BonusBand]) -> Decimal:
    """Rule 26: each band's percent on its own portion of the total, aggregate rounded HALF_UP to cents."""
    credit = Decimal(0)
    for band in bands:
        top = total if band.upper is None else min(total, band.upper)
        if top > band.lower:
            credit += (top - band.lower) * band.percent / HUNDRED
    return money(credit)


def allocate_cents(amount: Decimal, weights: dict[str, Decimal],
                   method: CentAllocation = CentAllocation.LARGEST_REMAINDER, tie_order: TieOrder = TieOrder.ASCENDING,
                   rest_base: Literal["half_up", "floor"] = "half_up") -> dict[str, Decimal]:
    """
    Split an amount (rounded to cents first) over positions proportionally to weights, in whole cents.

    Ties (equal remainders or equal weights) go by position order, ascending unless tie_order says otherwise.
    """
    total_weight = sum(weights.values(), Decimal(0))
    if not weights or total_weight <= 0:
        raise CalcError("allocation needs positive weights")
    cents = int(money(amount) / CENT)
    order = sorted(weights, key=position_key, reverse=tie_order is TieOrder.DESCENDING)
    exact = {p: Decimal(cents) * weights[p] / total_weight for p in order}
    if method is CentAllocation.LARGEST_REMAINDER:
        share = {p: int(exact[p].to_integral_value(ROUND_FLOOR)) for p in order}
        left = cents - sum(share.values())
        ranked = sorted(order, key=lambda p: exact[p] - share[p], reverse=True)  # stable: ties keep position order
        for p in ranked[:left]:
            share[p] += 1
    else:
        rounding = ROUND_FLOOR if (method is not CentAllocation.ROUND_EACH and rest_base == "floor") else ROUND_HALF_UP
        share = {p: int(exact[p].to_integral_value(rounding)) for p in order}
        if method is not CentAllocation.ROUND_EACH:
            target = {CentAllocation.REST_TO_FIRST: order[0], CentAllocation.REST_TO_LAST: order[-1],
                      CentAllocation.REST_TO_LARGEST: max(order, key=lambda p: weights[p])}[method]
            share[target] += cents - sum(share.values())
    return {p: Decimal(c) * CENT for p, c in share.items()}


# ---------------------------------------------------------------- the calculation


def _base_eur(pos: PositionInput, pieces: int, bid: BidInput) -> tuple[Decimal, PriceScale, PriceCondition, Decimal]:
    """Rule 22: price unit normalised, per-position scale, EUR per currency unit, HALF_UP to cents."""
    cond = valid_condition(pos.conditions, bid.scenario_date)
    scale = reached_scale(cond.scales, pieces)
    if cond.currency.upper() == "EUR":
        fx = Decimal(1)
    else:
        key = cond.fx_key or cond.currency
        if key not in bid.fx_rates and cond.currency not in bid.fx_rates:
            raise CalcError(f"no FX rate for {key}")
        fx = bid.fx_rates.get(key, bid.fx_rates.get(cond.currency))
    return money(scale.price / Decimal(cond.price_unit) * fx), scale, cond, fx


def _metal_rate(material: Material, bid: BidInput) -> Decimal:
    if material.surcharge_mass == 0:
        return Decimal(0)
    code = material.metal_index
    if code is None:
        if len(bid.metal_indices) != 1:
            raise CalcError(f"{material.material_number}: metal index code needed")
        code = next(iter(bid.metal_indices))
    if code not in bid.metal_indices:
        raise CalcError(f"no metal index {code}")
    return metal_rate(bid.metal_indices[code], bid.scenario_date)


def effective_discount(approval: Approval | None, order_value: Decimal | None) -> Decimal:
    """Rules 5 and 16: approved percent, or 0 when the inclusive threshold fails (or no value exists, as for no_bid)."""
    if approval is None:
        return Decimal(0)
    if approval.min_order_value is None:
        return approval.percent
    if order_value is None or order_value < approval.min_order_value:
        return Decimal(0)
    return approval.percent


def lead_time(pos: PositionInput, pooled_demand: int) -> int:
    """Rules 6, 15, 24: commitment if material matches and pooled demand is within its ceiling, else shared stock."""
    material = pos.material
    assert material is not None
    c = pos.commitment
    if c is not None and c.material_number == material.material_number and pooled_demand <= c.ceiling:
        return c.lead_time_weeks
    if material.free_stock >= pooled_demand:
        return material.stock_lead_time_weeks
    return material.production_lead_time_weeks


def calculate(bid: BidInput) -> BidResult:
    """Apply the bid rules to established facts and return the answer with intermediates."""
    missing = [p.position for p in bid.positions if p.material is None]
    if missing:
        return BidResult(tender_reference=bid.tender_reference, decision="no_bid", positions=[],
                         discount_percent=effective_discount(bid.approval, None), total_net=Decimal("0.00"),
                         missing_materials=missing)

    pieces = {p.position: to_pieces(p.quantity, p.unit, p.pieces_per_set) for p in bid.positions}
    pooled: dict[str, int] = defaultdict(int)
    for p in bid.positions:
        pooled[p.material.material_number] += pieces[p.position]  # type: ignore[union-attr]

    base: dict[str, tuple[Decimal, PriceScale, PriceCondition, Decimal]] = {
        p.position: _base_eur(p, pieces[p.position], bid) for p in bid.positions}
    rates = {p.position: _metal_rate(p.material, bid) for p in bid.positions}  # type: ignore[arg-type]
    surcharge = {p.position: p.material.surcharge_mass * rates[p.position] for p in bid.positions}  # type: ignore

    order_value = money(sum((pieces[k] * base[k][0] for k in pieces), Decimal(0)))
    discount = effective_discount(bid.approval, order_value)
    factor = 1 - discount / HUNDRED

    pool: dict[str, Decimal] = {}
    if bid.setup_pool is not None:
        by_pos = {p.position: p for p in bid.positions}
        weights = {m: pieces[m] * by_pos[m].material.allocation_mass  # type: ignore[union-attr]
                   for m in bid.setup_pool.members if m in by_pos}
        if weights:
            pool = allocate_cents(bid.setup_pool.fee, weights, bid.setup_pool.allocation, bid.setup_pool.tie_order,
                                  bid.setup_pool.rest_base)

    bonus: dict[str, Decimal] = {}
    bonus_total = None
    if bid.project_bonus is not None:
        goods = {}
        for p in bid.positions:
            if p.position in bid.project_bonus.members:
                unit = base[p.position][0] + (surcharge[p.position] if bid.project_bonus.include_surcharge else 0)
                goods[p.position] = pieces[p.position] * unit
        bonus_total = bonus_credit(money(sum(goods.values(), Decimal(0))), bid.project_bonus.bands)
        if goods and bonus_total > 0:
            bonus = allocate_cents(bonus_total, goods)

    positions, debug = [], []
    for p in bid.positions:
        k, material, n = p.position, p.material, pieces[p.position]
        assert material is not None
        base_eur, scale, cond, fx = base[k]
        f_base = factor if p.discount_scope is not DiscountScope.NONE else Decimal(1)
        f_sur = factor if p.discount_scope is DiscountScope.BASE_AND_SURCHARGE else Decimal(1)
        if n == 0:
            raise CalcError(f"{k}: zero pieces")
        cartons, carton_cost = 0, Decimal(0)
        if p.packaging is not None:
            if not material.pieces_per_carton:
                raise CalcError(f"{k}: carton size unknown")
            cartons = math.ceil(n / material.pieces_per_carton)
            carton_cost = cartons * p.packaging.price_per_carton
        setup = p.setup_fee + pool.get(k, Decimal(0))
        credit = bonus.get(k, Decimal(0))
        exact = base_eur * f_base + surcharge[k] * f_sur + (setup + carton_cost - credit) / n
        positions.append(AnswerPosition(
            position=k, material_number=material.material_number, quantity=n, unit_price=money(exact),
            lead_time_weeks=lead_time(p, pooled[material.material_number]), base_unit_price=base_eur,
            surcharge_per_unit=money(surcharge[k] + setup / n), packaging_per_unit=money(carton_cost / n),
            rebate_total=money(credit)))
        debug.append(PositionDebug(
            position=k, pieces=n, pooled_demand=pooled[material.material_number], scale_min_quantity=scale.min_quantity,
            scale_price=scale.price, currency=cond.currency, fx_rate=fx, base_eur=base_eur, metal_rate=rates[k],
            mass_surcharge=surcharge[k], discount_factor_base=f_base, discount_factor_surcharge=f_sur,
            setup_fee=p.setup_fee, pool_allocation=pool.get(k, Decimal(0)), cartons=cartons, carton_cost=carton_cost,
            bonus=credit, unit_price_exact=exact))

    total = money(sum((p.quantity * p.unit_price for p in positions), Decimal(0)))
    return BidResult(tender_reference=bid.tender_reference, decision="bid", discount_percent=discount,
                     positions=positions, total_net=total, order_value=order_value, bonus_total=bonus_total,
                     debug=debug)
