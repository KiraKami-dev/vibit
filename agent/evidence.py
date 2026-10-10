"""
Evidence records the research workers produce, and their mapping onto calc's input models.

Workers copy values exactly as the pages show them (raw strings, which the grounding check can verify); the parsing
here is deterministic: locale-tagged numbers ("1.234,50 (de-DE)", "12,345 (en-US)"), dates in several orders,
masses with units, lead times in weeks. Exact attribute matching is a validator over the model-collected facts.
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field

import calc

# ---------------------------------------------------------------- parsing


class ParseError(ValueError):
    pass


_TAG = re.compile(r"\(\s*([a-z]{2})[-_]([A-Z]{2})\s*\)")
_NUM = re.compile(r"[-+]?\d[\d.,'   ]*")


def number_format(text: str, fallback: str | None = None) -> str | None:
    """The locale a value is written in: its own tag "(de-DE)", else the fallback (e.g. the record's format)."""
    tag = _TAG.search(text or "")
    if tag:
        return tag.group(1).lower()
    if fallback:
        m = re.search(r"([a-z]{2})(?:[-_][A-Za-z]{2})?", fallback.strip(), re.IGNORECASE)
        return m.group(1).lower() if m else None
    return None


def parse_number(text: str, fmt: str | None = None) -> Decimal:
    """
    A number as a page writes it. `fmt` is a language code or locale ("de", "en-US") when the text carries no tag.

    Without any format: both separators -> the last one is decimal; a single comma or dot followed by exactly three
    digits is a thousands separator only for "en"/"de" respectively, otherwise German pages default to comma decimal.
    """
    if text is None:
        raise ParseError("no value")
    lang = number_format(text, fmt)
    body = _TAG.sub("", str(text))
    m = _NUM.search(body)
    if not m:
        raise ParseError(f"no number in {text!r}")
    raw = re.sub(r"['   ]", "", m.group(0)).rstrip(".,")
    if lang == "en":
        raw = raw.replace(",", "")
    elif lang in ("de", "fr", "es", "it", "nl", "pt"):
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".") if raw.count(",") == 1 else raw.replace(",", "")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    try:
        return Decimal(raw)
    except InvalidOperation as error:
        raise ParseError(f"cannot read {text!r}") from error


def parse_int(text: str, fmt: str | None = None) -> int:
    value = parse_number(text, fmt)
    if value != value.to_integral_value():
        raise ParseError(f"{text!r} is not a whole number")
    return int(value)


_DATE_TAG = re.compile(r"\((MM|TT|DD)[/.-](TT|DD|MM)\)", re.IGNORECASE)


def parse_date(text: str) -> date:
    """
    A date as shown: ISO "2027-01-25", German "25.01.2027", or slashed "01/25/2027" / "25/01/2027" with an optional
    order tag "(MM/TT)" / "(TT/MM)" that may stand between day-month and year ("21/03 (TT/MM) 2030").
    Untagged slashed dates are read day first unless that is impossible.
    """
    t = str(text)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", t)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"(\d{1,2})/(\d{1,2})(?:/|\s*(?:\([^)]*\))?\s*)(\d{4})", t)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        tag = _DATE_TAG.search(t)
        month_first = bool(tag and tag.group(1).upper() == "MM") or (not tag and a <= 12 < b)
        month, day = (a, b) if month_first else (b, a)
        return date(year, month, day)
    raise ParseError(f"no date in {text!r}")


_MASS_UNITS = {"kg": Decimal(1), "g": Decimal("0.001"), "t": Decimal(1000), "mg": Decimal("0.000001")}


def parse_mass_kg(text: str, fmt: str | None = None) -> Decimal:
    """A mass per piece in kg ("1250 g/Stück" -> 1.25). A bare number is taken as kg."""
    value = parse_number(text, fmt)
    unit = re.search(r"\d\s*(kg|mg|g|t)\b", _TAG.sub("", text), re.IGNORECASE)
    return value * _MASS_UNITS[unit.group(1).lower()] if unit else value


def parse_weeks(text: str) -> int:
    """A lead time in whole weeks ("3 Wochen", "3 weeks"); days are converted when they are whole weeks."""
    value = parse_number(text)
    if re.search(r"tag|day", text, re.IGNORECASE):
        if value % 7:
            raise ParseError(f"{text!r} is not whole weeks")
        value = value / 7
    return int(value)


NONE_WORDS = re.compile(r"^(|-|—|–|keine?( gefordert| erforderlich| angegeben)?|ohne|none|n/?a|nicht gefordert)$",
                        re.IGNORECASE)


def norm_text(value: str | None) -> str:
    """Comparable form of a text attribute: case, spacing, ß, dashes; empty or "no requirement" words become "none"."""
    t = " ".join(str(value or "").replace("ß", "ss").split()).strip(" .;").casefold()
    return "none" if NONE_WORDS.match(t) else t


# ---------------------------------------------------------------- specification


class SpecRevision(BaseModel):
    revision: str
    date_shown: str = ""
    status_shown: str = Field("", description="Status as the page shows it (incl. tooltips), e.g. current/superseded")


class Requirement(BaseModel):
    """One requirement of the governing specification revision, parsed."""
    requirement_id: str
    revision: str
    bore: Decimal
    stroke: Decimal = Field(description="As written; the usable travel when stroke_is_usable is set")
    pressure: Decimal
    mounting: str
    certification: str
    catalog_group: str
    pieces_per_set: int | None = None
    stroke_is_usable: bool = False
    reserve_key: str | None = None
    raw: dict[str, str] = Field(default_factory=dict, description="The values as the page showed them")


class SpecResult(BaseModel):
    document: str = ""
    revisions: list[SpecRevision] = Field(default_factory=list)
    current_revision: str = ""
    requirements: list[Requirement] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    seconds: float = 0


class EffectiveRequirement(BaseModel):
    """The attributes an ERP material must match, after amendments and the usable-travel derivation (rules 14, 20)."""
    requirement_id: str
    catalog_group: str
    bore: Decimal
    stroke: Decimal | None = Field(description="Nominal stroke to match; None while a usable-travel reserve is "
                                               "still unknown")
    pressure: Decimal
    mounting: str
    certification: str
    pending: list[str] = Field(default_factory=list, description="What is still unresolved")

    def attributes(self) -> dict[str, Decimal | str]:
        attrs: dict[str, Decimal | str] = {"bore": self.bore, "pressure": self.pressure,
                                           "mounting": norm_text(self.mounting),
                                           "certification": norm_text(self.certification)}
        if self.stroke is not None:
            attrs["stroke"] = self.stroke
        return attrs


def effective_requirement(req: Requirement, amendments: list[calc.Amendment] | None = None,
                          reserves: dict[str, Decimal] | None = None,
                          scenario_date: date | None = None) -> EffectiveRequirement:
    """
    Hooks for the other workers' facts: scoped amendments from the tender Q&A (already filtered to the right sender
    by the caller; revision and requirement scoping and latest-wins happen here) and the per-end reserve values of
    the revision-specific Technikfreigabe, by reserve key. Without a needed reserve the stroke stays open.
    """
    base: dict[str, Decimal | str] = {"bore": req.bore, "stroke": req.stroke, "pressure": req.pressure,
                                      "mounting": req.mounting, "certification": req.certification}
    attrs = calc.apply_amendments(base, amendments or [], req.revision, req.requirement_id, scenario_date)
    pending: list[str] = []
    stroke: Decimal | None = Decimal(str(attrs["stroke"]))
    if req.stroke_is_usable:
        reserve = (reserves or {}).get(req.reserve_key or "")
        if reserve is None:
            pending.append(f"per-end reserve for key {req.reserve_key or '(none given)'}")
            stroke = None
        else:
            stroke = calc.nominal_stroke(stroke, Decimal(str(reserve)))
    return EffectiveRequirement(requirement_id=req.requirement_id, catalog_group=req.catalog_group,
                                bore=Decimal(str(attrs["bore"])), stroke=stroke,
                                pressure=Decimal(str(attrs["pressure"])), mounting=str(attrs["mounting"]),
                                certification=str(attrs["certification"]), pending=pending)


# ---------------------------------------------------------------- ERP


class Candidate(BaseModel):
    """A search hit with the attributes the list (or its detail page) shows."""
    material_number: str
    link: str = ""
    bore: Decimal | None = None
    stroke: Decimal | None = None
    pressure: Decimal | None = None
    mounting: str = ""
    certification: str = ""
    description: str = ""

    def attributes(self) -> dict[str, Decimal | str]:
        attrs: dict[str, Decimal | str] = {"mounting": norm_text(self.mounting),
                                           "certification": norm_text(self.certification)}
        for key in ("bore", "stroke", "pressure"):
            if getattr(self, key) is not None:
                attrs[key] = getattr(self, key)
        return attrs


def matches(req: EffectiveRequirement, attrs: dict[str, Decimal | str]) -> bool:
    """Rule 3 as a validator: every resolved attribute equal (numbers as numbers, texts normalised)."""
    want = req.attributes()
    return bool(calc.matching_materials(want, [("x", attrs)], keys=tuple(want)))


class MaterialRecord(BaseModel):
    """Everything the ERP shows for one material that the bid needs, parsed."""
    material_number: str
    active: bool
    status: str = ""
    bore: Decimal | None = None
    stroke: Decimal | None = None
    pressure: Decimal | None = None
    mounting: str = ""
    certification: str = ""
    free_stock: int | None = None
    blocked_stock: int | None = None
    stock_lead_time_weeks: int | None = None
    production_lead_time_weeks: int | None = None
    pieces_per_carton: int | None = None
    allocation_mass_kg: Decimal | None = None
    surcharge_mass_kg: Decimal | None = None
    metal_index: str | None = None
    conditions: list[calc.PriceCondition] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)

    def attributes(self) -> dict[str, Decimal | str]:
        return Candidate(material_number=self.material_number, bore=self.bore, stroke=self.stroke,
                         pressure=self.pressure, mounting=self.mounting,
                         certification=self.certification).attributes()

    def to_material(self) -> calc.Material:
        missing = [k for k in ("stock_lead_time_weeks", "production_lead_time_weeks") if getattr(self, k) is None]
        if missing:
            raise calc.CalcError(f"{self.material_number}: {', '.join(missing)} unknown")
        return calc.Material(material_number=self.material_number, free_stock=self.free_stock or 0,
                             stock_lead_time_weeks=self.stock_lead_time_weeks,  # type: ignore[arg-type]
                             production_lead_time_weeks=self.production_lead_time_weeks,  # type: ignore[arg-type]
                             pieces_per_carton=self.pieces_per_carton,
                             allocation_mass=self.allocation_mass_kg or Decimal(0),
                             surcharge_mass=self.surcharge_mass_kg or Decimal(0), metal_index=self.metal_index)


class RequirementResearch(BaseModel):
    """ERP findings for one requirement: what was searched, the candidates, and the matching active material."""
    requirement_id: str
    search_term: str = ""
    candidates: list[Candidate] = Field(default_factory=list)
    inspected: list[str] = Field(default_factory=list)
    selected: str | None = Field(default=None, description="The exactly matching active material, if any")
    problems: list[str] = Field(default_factory=list)


def merged_attributes(record: MaterialRecord, research: RequirementResearch) -> dict[str, Decimal | str]:
    """A material's attributes: the detail page's where it showed them, else the search list's."""
    cand = next((c for c in research.candidates if c.material_number == record.material_number), None)
    attrs = cand.attributes() if cand else {}
    for key in ("bore", "stroke", "pressure"):
        if getattr(record, key) is not None:
            attrs[key] = getattr(record, key)
    for key in ("mounting", "certification"):
        if getattr(record, key):
            attrs[key] = norm_text(getattr(record, key))
    return attrs


def mismatches(req: EffectiveRequirement, attrs: dict[str, Decimal | str]) -> list[str]:
    """Rule 3 as a validator: the attributes in which a material differs from the requirement (or does not show)."""
    out = []
    for key, value in req.attributes().items():
        if key not in attrs:
            out.append(f"{key} not shown")
        elif not calc.matching_materials({key: value}, [("x", {key: attrs[key]})], keys=(key,)):
            out.append(f"{key} {attrs[key]} instead of {value}")
    return out


def check_choice(req: EffectiveRequirement, research: RequirementResearch, materials: dict[str, MaterialRecord],
                 chosen: str | None) -> str | None:
    """
    Validate a model's material choice; None when it holds, else why not. A chosen material must be inspected,
    active and equal in every attribute; "none" must not leave out an active material that equals in every one.
    """
    if chosen:
        record = materials.get(chosen)
        if record is None:
            return f"{chosen} is not among the inspected materials {sorted(materials)}"
        if not record.active:
            return f"{chosen} is not active (status {record.status})"
        diff = mismatches(req, merged_attributes(record, research))
        return f"{chosen} does not match exactly: {'; '.join(diff)}" if diff else None
    fits = [n for n, r in materials.items() if r.active and not mismatches(req, merged_attributes(r, research))]
    return f"{', '.join(fits)} is active and equals the requirement in every attribute; check again" if fits else None


def to_position_input(position: str, quantity: int, unit: str | calc.Unit, requirement: Requirement,
                      material: MaterialRecord | None, **extra) -> calc.PositionInput:
    """One position for calc: quantity in its stated unit (from the tender worker), spec pieces per set, the ERP
    material and its price conditions. `extra` passes the Q&A/mail facts (discount_scope, setup_fee, packaging,
    commitment) through."""
    return calc.PositionInput(
        position=position, quantity=quantity, unit=unit if isinstance(unit, calc.Unit) else calc.parse_unit(unit),
        pieces_per_set=requirement.pieces_per_set, material=material.to_material() if material else None,
        conditions=material.conditions if material else [], **extra)
