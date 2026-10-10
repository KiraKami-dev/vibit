"""
From findings to what gets entered: one entry per tender position, filled where the facts are complete and omitted
with a reason where they are not. Pure and model-free; the bid rules themselves live in `calc.py`.

This build prices a position as base price less the order discount. Surcharge, setup, packaging and bonus are not
applied yet.
"""
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

import calc
from evidence import Candidate, ErpFindings, Findings, MaterialFacts, Requirement, TenderFindings, TenderPosition
from parse import ParseError, parse_date, parse_datetime, parse_decimal, parse_int, parse_unit, split_address


class PlanEntry(BaseModel):
    position: str
    omit: bool = Field(description="True: leave the position out of the bid; `reason` says what was missing")
    reason: str = ""
    material_number: str = ""
    # the chosen material's attributes as the ERP writes them, to pick it in the bid form
    bore: str = ""
    stroke: str = ""
    pressure: str = ""
    mounting: str = ""
    certification: str = ""
    quantity: int | None = Field(default=None, description="Final quantity in pieces")
    lead_time_weeks: int | None = None
    unit_price: Decimal | None = Field(default=None, description="Final net unit price in EUR")
    base_unit_price: Decimal | None = None


class BidPlan(BaseModel):
    tender_reference: str
    decision: Literal["bid", "no_bid"] = "bid"
    discount_percent: Decimal = Decimal(0)
    total_net: Decimal = Decimal("0.00")
    entries: list[PlanEntry] = []
    notes: list[str] = Field(default_factory=list, description="Why something was omitted or assumed, for the trace")


class _Gap(calc.CalcError):
    """A fact this position needs was not established; the message is the omission reason."""


NUMERIC_KEYS = ("bore", "stroke", "pressure")  # compared as numbers; mounting and certification as text
_UNREADABLE = "\0unreadable: "  # marks an attribute value that could not be parsed, until the position needs it
_POSITION_WORD = r"(?:position|pos\.?|nr\.?|no\.?|#)"
_REVISION_WORD = r"(?:revision|rev\.?)"


def _ref(text: str, word: str = "") -> str:
    """An identifier for comparison: case and whitespace dropped, plus a leading label word such as "Pos."."""
    key = "".join(text.split()).casefold()
    if word:
        key = re.sub(rf"^{word}(?=.)", "", key)
    return key


def _position_ref(text: str) -> str:
    return re.sub(r"^0+(?=\d)", "", _ref(text, _POSITION_WORD).rstrip("."))


def _not_after(moment: datetime, scenario: datetime) -> bool:
    """Rule 21: dated no later than the scenario date. A scenario given as a bare day (midnight) covers that day."""
    if scenario.time() == time(0):
        return moment < scenario + timedelta(days=1)
    return moment <= scenario


def _labelled(label: str, parse, *args):
    """Run a parser and name the field in its error."""
    try:
        return parse(*args)
    except ParseError as e:
        raise ParseError(f"{label}: {e}") from e


def _short(error: Exception) -> str:
    return str(error).splitlines()[0]


def _attributes(shown: dict[str, str], number_format: str) -> dict[str, Decimal | str]:
    """Match attributes as values: numbers for bore, stroke and pressure, text otherwise."""
    out: dict[str, Decimal | str] = {}
    for name, value in shown.items():
        key = name.strip().casefold()
        if key in NUMERIC_KEYS:
            try:
                out[key] = parse_decimal(value, number_format)
            except ParseError as e:
                out[key] = _UNREADABLE + str(e)
        else:
            out[key] = value
    return out


def _unreadable(value: Decimal | str) -> bool:
    return isinstance(value, str) and value.startswith(_UNREADABLE)


# ---------------------------------------------------------------- tender Q&A


class _QA:
    """The Q&A records that apply on the scenario date, in the shape the calc helpers take."""

    def __init__(self, tender: TenderFindings, scenario: datetime, notes: list[str]):
        self.shown = tender.quantity_changes
        self.changes: list[calc.QuantityChange] = []  # `quantity` holds the index into `shown`, parsed when it wins
        self.withdrawn: set[str] = set()
        self.amendments: list[calc.Amendment] = []
        self.skipped = False  # a record could not be read, so "nothing else applies" is not certain

        for w in tender.withdrawals:
            if not w.date.strip():
                notes.append(f"withdrawal {w.answer} has no date: treated as in force")
                self.withdrawn.add(_ref(w.withdraws))
                continue
            when = self._when(w, f"withdrawal {w.answer}", notes)
            if when is not None and _not_after(when, scenario):
                self.withdrawn.add(_ref(w.withdraws))
        for index, c in enumerate(tender.quantity_changes):
            when = self._when(c, f"quantity change {c.answer}", notes)
            if when is not None and _not_after(when, scenario):
                self.changes.append(calc.QuantityChange(number=_ref(c.answer), date=when,
                                                        position=_position_ref(c.position), quantity=index))
        for a in tender.amendments:
            when = self._when(a, f"amendment {a.answer}", notes)
            if when is not None and _not_after(when, scenario):
                self.amendments.append(calc.Amendment(
                    date=when, revision=_ref(a.revision, _REVISION_WORD), requirement=_ref(a.requirement),
                    attributes=_attributes(a.attributes, a.number_format)))

    def _when(self, record, label: str, notes: list[str]) -> datetime | None:
        try:
            return parse_datetime(record.date, record.date_order, record.timezone)
        except ParseError as e:
            notes.append(f"{label} skipped: {e}")
            self.skipped = True
            return None


# ---------------------------------------------------------------- mail authority


class _Mail(calc.DatedRecord):
    index: int


def _governing(records: list, sender_local: str, reference: str, scenario: datetime, domain: str, label: str,
               notes: list[str]) -> int | None:
    """Rule 21: index of the latest record for the exact reference from sender_local@domain, not after the scenario."""
    dated = []
    for index, r in enumerate(records):
        try:
            local, sender_domain = split_address(r.sender)
            when = parse_datetime(r.date, r.date_order, r.timezone)
        except ParseError as e:
            if r.reference.strip() == reference.strip():
                notes.append(f"{label} mail skipped: {e}")
            continue
        if _not_after(when, scenario):
            dated.append(_Mail(reference=r.reference, sender=f"{local}@{sender_domain}", date=when, index=index))
    hit = calc.latest_authoritative(dated, reference, date.max, sender_local, domain)  # dates are filtered above
    return None if hit is None else hit.index


def _approval(findings: Findings, reference: str, scenario: datetime, domain: str,
              notes: list[str]) -> calc.Approval | None:
    """Rules 5 and 16: the governing director approval. An unreadable one counts as none, never an older one."""
    approving = [a for a in findings.mail.approvals if a.approves]
    index = _governing(approving, "leitung", reference, scenario, domain, "approval", notes)
    if index is None:
        notes.append("no discount approval applies: discount 0")
        return None
    shown = approving[index]
    try:
        percent = _labelled("approved percent", parse_decimal, shown.percent, shown.number_format)
        minimum = None
        if shown.min_order_value.strip():
            minimum = _labelled("minimum order value", parse_decimal, shown.min_order_value, shown.number_format)
        if not 0 <= percent <= 100:
            raise ParseError(f"approved percent {percent} out of range")
    except ParseError as e:
        notes.append(f"governing approval unreadable ({e}): discount 0")
        return None
    return calc.Approval(percent=percent, min_order_value=minimum)


@dataclass
class _Fx:
    rates: dict[str, Decimal]  # FX key or currency, upper-cased -> EUR per unit
    unreadable: dict[str, str]
    found: bool


def _fx(findings: Findings, reference: str, scenario: datetime, domain: str, notes: list[str]) -> _Fx:
    """Rule 22: the rates of the governing FX fixing (sent, exact reference, finance sender, not after the scenario)."""
    sent = [f for f in findings.mail.fx_fixings if f.sent]
    index = _governing(sent, "finanzen", reference, scenario, domain, "FX fixing", notes)
    fx = _Fx(rates={}, unreadable={}, found=index is not None)
    if index is not None:
        for key, value in sent[index].rates.items():
            try:
                fx.rates[key.strip().upper()] = parse_decimal(value, sent[index].number_format)
            except ParseError as e:
                fx.unreadable[key.strip().upper()] = str(e)
    return fx


@dataclass
class _Releases:
    reserves: dict[str, Decimal]  # reserve key, upper-cased -> reserve per end
    unreadable: dict[str, str]
    found: bool


def _releases(findings: Findings, reference: str, revision: str, scenario: datetime, domain: str,
              notes: list[str]) -> _Releases:
    """Rules 20 and 21: the reserves of the governing technical release for the current specification revision."""
    scoped = [r for r in findings.mail.technical_releases if revision and _ref(r.revision, _REVISION_WORD) == revision]
    index = _governing(scoped, "technik", reference, scenario, domain, "technical release", notes)
    out = _Releases(reserves={}, unreadable={}, found=index is not None)
    if index is not None:
        for key, value in scoped[index].reserves.items():
            try:
                out.reserves[key.strip().upper()] = parse_decimal(value, scoped[index].number_format)
            except ParseError as e:
                out.unreadable[key.strip().upper()] = str(e)
    return out


# ---------------------------------------------------------------- one position


@dataclass
class _State:
    tender: TenderPosition
    reason: str = ""  # first missing fact; empty = nothing missing so far
    pieces: int | None = None
    material: Candidate | None = None
    facts: MaterialFacts | None = None
    lead_time: int | None = None
    base: Decimal | None = None
    condition: calc.PriceCondition | None = None
    certain_no_match: bool = False

    def gap(self, error: Exception) -> None:
        self.reason = self.reason or _short(error)


def _pieces(tender: TenderPosition, requirement: Requirement | None, qa: _QA) -> int:
    """Rules 1, 13, 17: the tender quantity or the governing change, in pieces."""
    quantity = _labelled("tender quantity", parse_int, tender.quantity, tender.number_format)
    unit_text = tender.unit
    winner = calc.resolve_quantity(-1, _position_ref(tender.position), qa.changes, qa.withdrawn)
    if winner >= 0:
        change = qa.shown[winner]
        quantity = _labelled(f"quantity of change {change.answer}", parse_int, change.quantity, change.number_format)
        unit_text = change.unit if change.unit.strip() else unit_text
    unit = parse_unit(unit_text)
    per_set = None
    if unit is calc.Unit.SET and requirement is not None and requirement.pieces_per_set.strip():
        per_set = _labelled("pieces per set", parse_int, requirement.pieces_per_set, requirement.number_format)
    pieces = calc.to_pieces(quantity, unit, per_set)
    if pieces <= 0:
        raise _Gap(f"quantity is {pieces}")
    return pieces


def _reserve(requirement: Requirement, revision: str, releases: _Releases) -> Decimal:
    """Rule 20: the per-end reserve the governing technical release of this revision states for the requirement."""
    if not releases.found:
        raise _Gap("stroke is given as usable travel and no technical release for the revision applies")
    key = requirement.reserve_key.strip().upper()
    if key in releases.unreadable:
        raise ParseError(f"end reserve {key}: {releases.unreadable[key]}")
    if key not in releases.reserves:
        if not key and len(releases.reserves) == 1:
            return next(iter(releases.reserves.values()))
        raise _Gap(f"the technical release states no end reserve for key {key or '(none)'}")
    return releases.reserves[key]


def _requirement(requirement: Requirement, revision: str, qa: _QA, releases: _Releases) -> dict[str, Decimal | str]:
    """Rules 14 and 20: the requirement's match attributes after the amendments scoped to this revision and
    requirement, with a usable stroke turned into the nominal one."""
    key = _ref(requirement.requirement)
    if not revision and any(a.requirement == key for a in qa.amendments):
        raise _Gap("current specification revision unknown, so amendments cannot be scoped")
    shown = {k: getattr(requirement, k) for k in calc.MATCH_KEYS}
    attributes = calc.apply_amendments(_attributes(shown, requirement.number_format), qa.amendments, revision, key)
    for name in calc.MATCH_KEYS:
        value = attributes[name]
        if _unreadable(value):
            raise ParseError(f"requirement {name}: {str(value).removeprefix(_UNREADABLE)}")
    if requirement.usable_stroke:
        attributes["stroke"] = calc.nominal_stroke(attributes["stroke"], _reserve(requirement, revision, releases))
    return attributes


def _material(state: _State, required: dict[str, Decimal | str], erp: ErpFindings | None,
              notes: list[str]) -> Candidate:
    """Rule 3: the model's choice, cross-checked: among the candidates, active and equal on every match attribute."""
    if erp is None:
        raise _Gap("no ERP lookup for the requirement")
    if not erp.candidates:
        raise _Gap("no ERP candidates read for the requirement")
    listed = []
    for c in erp.candidates:
        attributes = _attributes({k: getattr(c, k) for k in calc.MATCH_KEYS}, c.number_format)
        listed.append({k: v for k, v in attributes.items() if not _unreadable(v)})  # unreadable never matches
    active = [(str(i), listed[i]) for i, c in enumerate(erp.candidates) if c.active is True]
    matches = [erp.candidates[int(i)] for i in calc.matching_materials(required, active)]
    chosen = _ref(erp.chosen)
    for c in matches:
        if _ref(c.material_number) == chosen:
            return c
    position = state.tender.position
    if len(matches) == 1:
        notes.append(f"position {position}: chosen material {erp.chosen or '(none)'} fails the cross-check; "
                     f"{matches[0].material_number} is the only active candidate that matches and is used")
        return matches[0]
    if matches:
        raise _Gap(f"several active candidates match and the chosen material {erp.chosen or '(none)'} is not one")

    # Wording is the model's call: two sources may name the same mounting or certification differently, so a chosen
    # active material that equals the requirement on every number is accepted on the model's reading of the text.
    numbers = tuple(k for k in calc.MATCH_KEYS if k in NUMERIC_KEYS)
    picked = next((c for c in erp.candidates if _ref(c.material_number) == chosen), None)
    if picked is not None and picked.active is True and calc.matching_materials(
            required, [("", listed[erp.candidates.index(picked)])], keys=numbers):
        notes.append(f"position {position}: chosen material {erp.chosen} equals the requirement on every number; "
                     "mounting or certification are worded differently and taken on the model's comparison")
        return picked

    # nothing matches: sure enough for no_bid only if the model chose none as well and every candidate is known
    # blocked or differs on a known value
    def differs(attributes: dict[str, Decimal | str]) -> bool:
        return any(k in attributes and attributes[k] != "" and required[k] != ""
                   and not calc.matching_materials(required, [("", attributes)], keys=(k,)) for k in calc.MATCH_KEYS)

    state.certain_no_match = (not chosen and erp.all_candidates_read
                              and all(required[k] != "" for k in calc.MATCH_KEYS)
                              and all(c.active is False or differs(listed[i]) for i, c in enumerate(erp.candidates)))
    if not chosen:
        raise _Gap("no material chosen and no active candidate matches the requirement")
    if picked is None:
        raise _Gap(f"chosen material {erp.chosen} is not among the candidates")
    if picked.active is not True:
        raise _Gap(f"chosen material {erp.chosen} is not active" if picked.active is False
                   else f"status of chosen material {erp.chosen} unknown")
    raise _Gap(f"chosen material {erp.chosen} does not match the requirement exactly")


def _weeks(label: str, text: str, number_format: str) -> int:
    """A lead time in weeks; one stated in days counts only if it is whole weeks."""
    value = _labelled(label, parse_int, text, number_format)
    if re.search(r"\b(tage?n?|days?)\b", text, re.IGNORECASE):
        if value % 7:
            raise ParseError(f"{label}: {text.strip()!r} is not whole weeks")
        return value // 7
    return value


def _lead_time(facts: MaterialFacts, pooled: int, open_demand: bool) -> int:
    """Rules 6 and 15: stock lead time if free stock covers the material's pooled demand, else production."""
    stock = _labelled("free stock", parse_int, facts.free_stock, facts.number_format)
    # ask calc which of the two lead times applies (0 = stock, 1 = production), then read only that one
    probe = calc.Material(material_number=facts.material_number, free_stock=stock, stock_lead_time_weeks=0,
                          production_lead_time_weeks=1)
    from_stock = calc.lead_time(calc.PositionInput(position="", quantity=0, material=probe), pooled) == 0
    if from_stock and open_demand:
        raise _Gap("pooled demand unknown: another position with this material has no quantity")
    if from_stock:
        return _weeks("stock lead time", facts.stock_lead_time_weeks, facts.number_format)
    return _weeks("production lead time", facts.production_lead_time_weeks, facts.number_format)


def _condition(facts: MaterialFacts, day: date, fx: _Fx, position: str, notes: list[str]) -> calc.PriceCondition:
    """Rule 4: the price condition valid on the scenario day, parsed. Other conditions only need readable dates."""
    dated = []
    for c in facts.conditions:
        start = _labelled("price validity start", parse_date, c.valid_from, c.date_order)
        open_end = not re.search(r"\d", c.valid_to)  # blank or a word such as "open": no end
        end = None if open_end else _labelled("price validity end", parse_date, c.valid_to, c.date_order)
        dated.append(calc.PriceCondition(valid_from=start, valid_to=end, scales=[]))
    valid = calc.valid_condition(dated, day)
    shown = next(c for c, d in zip(facts.conditions, dated) if d is valid)
    number_format = shown.number_format or facts.number_format

    scales = []
    for s in shown.scales:
        lone_price = len(shown.scales) == 1 and not s.min_quantity.strip()  # one price without a scale table
        minimum = 0 if lone_price else _labelled("scale quantity", parse_int, s.min_quantity, number_format)
        scales.append(calc.PriceScale(min_quantity=minimum, price=_labelled("price", parse_decimal, s.price,
                                                                            number_format)))
    currency = shown.currency.strip().upper().replace("€", "EUR")
    if not currency and any("€" in s.price or "EUR" in s.price.upper() for s in shown.scales):
        currency = "EUR"
    if not currency:
        raise _Gap("currency of the price condition unknown")
    if shown.price_unit.strip():
        price_unit = _labelled("price unit", parse_int, shown.price_unit, number_format)
    else:
        price_unit = 1
        notes.append(f"position {position}: no price unit stated, assumed 1 piece")
    fx_key = shown.fx_key.strip().upper() or None
    if currency != "EUR":
        key = fx_key or currency
        if key in fx.unreadable:
            raise ParseError(f"FX rate {key}: {fx.unreadable[key]}")
        if key not in fx.rates and currency not in fx.rates:
            raise _Gap(f"no usable FX fixing for {key}" if not fx.found else f"the FX fixing has no rate for {key}")
    return calc.PriceCondition(valid_from=valid.valid_from, valid_to=valid.valid_to, currency=currency, fx_key=fx_key,
                               price_unit=price_unit, scales=scales)


def _bid_input(states: list[_State], reference: str, day: date, fx: _Fx,
               approval: calc.Approval | None = None) -> calc.BidInput:
    """Priced positions as calc input. Lead times are decided here (pooled over all positions), so calc gets them
    fixed; the stock is left at 0 and both lead times set to the decided one."""
    positions = []
    for s in states:
        assert s.material is not None and s.pieces is not None and s.condition is not None
        weeks = s.lead_time or 0
        material = calc.Material(material_number=s.material.material_number, stock_lead_time_weeks=weeks,
                                 production_lead_time_weeks=weeks)
        positions.append(calc.PositionInput(position=s.tender.position, quantity=s.pieces, material=material,
                                            conditions=[s.condition]))
    return calc.BidInput(tender_reference=reference, scenario_date=day, positions=positions, approval=approval,
                         fx_rates=fx.rates)


# ---------------------------------------------------------------- the plan


def build_plan(findings: Findings, tender_reference: str, scenario_date: datetime, bidder_domain: str) -> BidPlan:
    """The bid the findings support. `scenario_date` is timezone-aware; `bidder_domain` is the bidder mailbox's
    domain, which authorises senders."""
    notes: list[str] = []
    scenario = scenario_date if scenario_date.tzinfo else scenario_date.replace(tzinfo=UTC)
    day = scenario.date()  # the scenario's own calendar day, for validity windows written as plain dates
    domain = bidder_domain.strip().lstrip("@").lower()

    states: list[_State] = []
    seen: set[str] = set()
    for tender in findings.tender.positions:
        key = _position_ref(tender.position)
        if key in seen:
            notes.append(f"position {tender.position} listed twice: first entry kept")
            continue
        seen.add(key)
        states.append(_State(tender=tender))
    if not states:
        notes.append("no tender positions found")

    qa = _QA(findings.tender, scenario, notes)
    approval = _approval(findings, tender_reference, scenario, domain, notes)
    fx = _fx(findings, tender_reference, scenario, domain, notes)
    requirements = {_ref(r.requirement): r for r in findings.spec.requirements}
    lookups = {_ref(key): e for key, e in findings.erp.items()} | {
        _ref(e.requirement): e for e in findings.erp.values() if e.requirement.strip()}
    revision = _ref(findings.spec.current_revision, _REVISION_WORD)
    releases = _releases(findings, tender_reference, revision, scenario, domain, notes)

    # quantity and material are established independently, so one missing fact does not hide the other
    for s in states:
        requirement = requirements.get(_ref(s.tender.requirement)) if s.tender.requirement.strip() else None
        try:
            s.pieces = _pieces(s.tender, requirement, qa)
        except ValueError as e:
            s.gap(e)
        try:
            if requirement is None:
                raise _Gap(f"requirement {s.tender.requirement or '(none)'} not found in the specification")
            required = _requirement(requirement, revision, qa, releases)
            erp = lookups.get(_ref(requirement.requirement))
            s.material = _material(s, required, erp, notes)
            facts = erp.facts if erp is not None else None
            if facts is None or _ref(facts.material_number) != _ref(s.material.material_number):
                raise _Gap(f"no stock and price facts for material {s.material.material_number}")
            s.facts = facts
        except ValueError as e:
            s.gap(e)

    # rules 3 and 8: no_bid only when it is certain that a position has no matching active material
    sure = findings.tender.complete and findings.spec.complete and bool(revision) and not qa.skipped
    unmatched = [s for s in states if s.certain_no_match]
    if unmatched and sure:
        numbers = ", ".join(s.tender.position for s in unmatched)
        notes.append(f"no_bid: position {numbers} has no exactly matching active material; tender and specification "
                     "were read completely and every ERP candidate was checked")
        entries = [PlanEntry(position=s.tender.position, omit=True,
                             reason=s.reason if s.certain_no_match else "no_bid") for s in states]
        return BidPlan(tender_reference=tender_reference, decision="no_bid", entries=entries, notes=notes,
                       discount_percent=calc.effective_discount(approval, None))
    for s in unmatched:
        notes.append(f"position {s.tender.position}: no matching active material, but the findings are not complete "
                     "enough for no_bid; position omitted instead")

    # rule 15: demand is pooled per material over every position that selects it, priced or not
    pooled: dict[str, int] = defaultdict(int)
    open_demand: set[str] = set()
    for s in states:
        if s.material is not None:
            key = _ref(s.material.material_number)
            pooled[key] += s.pieces or 0
            if s.pieces is None:
                open_demand.add(key)

    for s in states:
        if s.material is None or s.facts is None or s.pieces is None:
            continue
        key = _ref(s.material.material_number)
        try:
            s.lead_time = _lead_time(s.facts, pooled[key], key in open_demand)
        except ValueError as e:
            s.gap(e)
        try:  # rule 22, through calc: valid condition, this position's scale, price unit, FX, HALF_UP to cents
            s.condition = _condition(s.facts, day, fx, s.tender.position, notes)
            alone = calc.calculate(_bid_input([s], tender_reference, day, fx))
            s.base = alone.positions[0].base_unit_price
        except ValueError as e:
            s.condition = None
            s.gap(e)

    # rule 16: the threshold looks at all tender positions; with a price missing only the known part can prove it
    priced = [s for s in states if s.base is not None and s.pieces is not None]
    order_value = calc.money(sum((s.pieces * s.base for s in priced), Decimal(0)))  # type: ignore[operator]
    discount = calc.effective_discount(approval, order_value)
    if approval is not None and approval.min_order_value is not None:
        if discount:
            notes.append(f"order value {order_value} reaches the approval's minimum {approval.min_order_value}")
        elif len(priced) < len(states):
            notes.append(f"known order value {order_value} is below the approval's minimum "
                         f"{approval.min_order_value} and some base prices are unknown: discount 0")
        else:
            notes.append(f"order value {order_value} is below the approval's minimum {approval.min_order_value}: "
                         "discount 0")

    filled = [s for s in states if not s.reason]
    total = Decimal("0.00")
    prices: dict[str, Decimal] = {}
    if filled:
        try:  # unit price = base x (1 - discount), rounded once; total = sum of quantity x rounded unit price
            decided = calc.Approval(percent=discount) if discount else None
            result = calc.calculate(_bid_input(filled, tender_reference, day, fx, decided))
            prices = {p.position: p.unit_price for p in result.positions}
            total = result.total_net
        except ValueError as e:  # not expected: every input was priced alone above
            for s in filled:
                s.gap(e)
        notes.append("surcharge, setup, packaging and bonus are not applied yet: unit price is base price less "
                     "discount")

    entries = []
    for s in states:
        if s.reason or s.material is None:
            notes.append(f"position {s.tender.position} omitted: {s.reason}")
            entries.append(PlanEntry(position=s.tender.position, omit=True, reason=s.reason))
            continue
        m = s.material
        entries.append(PlanEntry(
            position=s.tender.position, omit=False, material_number=m.material_number, bore=m.bore, stroke=m.stroke,
            pressure=m.pressure, mounting=m.mounting, certification=m.certification, quantity=s.pieces,
            lead_time_weeks=s.lead_time, unit_price=prices[s.tender.position], base_unit_price=s.base))
    return BidPlan(tender_reference=tender_reference, decision="bid", discount_percent=discount, total_net=total,
                   entries=entries, notes=notes)
