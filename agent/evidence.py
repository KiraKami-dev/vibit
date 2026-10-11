"""
Evidence records the research workers produce, and their mapping onto calc's input models.

Workers copy values exactly as the pages show them (raw strings, which the grounding check can verify); the parsing
here is deterministic: locale-tagged numbers ("1.234,50 (de-DE)", "12,345 (en-US)"), dates in several orders,
masses with units, lead times in weeks. Exact attribute matching is a validator over the model-collected facts.
"""
import re
from datetime import date, datetime
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


_SET_WORDS = ("satz", "sätze", "saetze", "set", "kit", "pack")


def parse_pieces_per_set(text: str) -> int:
    """
    Pieces in one set, from a model's copy of the spec wording: "5", "5 Stück", or a definition such as
    "1 Satz = 5 Stück" / "2 sets = 10 pcs" (the first number there is the sets, not the pieces: reading it as the
    pieces bids a fraction of the quantity). An equation gives right / left; otherwise a number next to a set word is the
    set count and the other one the pieces. Raises ParseError when the text does not say it unambiguously.
    """
    body = _TAG.sub("", str(text or ""))
    numbers = [int(n) for n in re.findall(r"\d+", body)]
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        sets, pieces = numbers
        equation = re.fullmatch(r"\D*\d+\D*[=:]\D*\d+\D*", body)
        if not equation:
            words = re.findall(r"(\d+)\s*([^\W\d_]*)", body)
            is_set = [w.casefold().startswith(_SET_WORDS) for _, w in words]
            if is_set == [False, True]:
                sets, pieces = numbers[1], numbers[0]
            elif is_set != [True, False]:
                raise ParseError(f"pieces per set unclear in {text!r}")
        if sets > 0 and pieces % sets == 0:
            return pieces // sets
    raise ParseError(f"pieces per set unclear in {text!r}")


_DATE_TAG = re.compile(r"\((MM|TT|DD)[/.-](TT|DD|MM)\)", re.IGNORECASE)


_MONTHS = {
    "januar": 1, "jänner": 1, "jaenner": 1, "january": 1, "jan": 1, "februar": 2, "february": 2, "feb": 2,
    "märz": 3, "maerz": 3, "march": 3, "mär": 3, "mrz": 3, "mar": 3, "april": 4, "apr": 4, "mai": 5, "may": 5,
    "juni": 6, "june": 6, "jun": 6, "juli": 7, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sept": 9, "sep": 9, "oktober": 10, "october": 10, "okt": 10, "oct": 10, "november": 11, "nov": 11,
    "dezember": 12, "december": 12, "dez": 12, "dec": 12}
_MONTH = "(" + "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True)) + r")(?![^\W\d_])\.?"


def _year(text: str) -> int:
    """A four-digit year as is; a two-digit one in this century ("27" -> 2027)."""
    return int(text) + 2000 if len(text) == 2 else int(text)


def parse_date(text: str) -> date:
    """
    A date as shown: ISO "2027-01-25", German "25.01.2027" / "25.01.27", slashed "01/25/2027" / "25/01/27" with an
    optional order tag "(MM/TT)" / "(TT/MM)" that may stand between day-month and year ("21/03 (TT/MM) 2030"), or
    with a German or English month name ("25. Januar 2027", "25 Jan 27", "January 25, 2027"). Two-digit years are
    this century's. Untagged slashed dates are read day first unless that is impossible.
    """
    t = str(text)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)", t)
    if m:
        return date(_year(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/|\s*(?:\([^)]*\))?\s*)(\d{4})(?!\d)", t) or \
        re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{2})(?!\d)", t)
    if m:
        a, b, year = int(m.group(1)), int(m.group(2)), _year(m.group(3))
        tag = _DATE_TAG.search(t)
        month_first = bool(tag and tag.group(1).upper() == "MM") or (not tag and a <= 12 < b)
        month, day = (a, b) if month_first else (b, a)
        return date(year, month, day)
    low = t.casefold()
    m = re.search(r"(?<!\d)(\d{1,2})\.?\s*" + _MONTH + r",?\s*(\d{4}|\d{2})(?!\d)", low)
    if m:
        return date(_year(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"(?<![^\W\d_])" + _MONTH + r"\s*(\d{1,2})(?:st|nd|rd|th|\.)?,?\s*(\d{4}|\d{2})(?!\d)", low)
    if m:
        return date(_year(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
    raise ParseError(f"no date in {text!r}")


_MASS_UNITS = {"kg": Decimal(1), "g": Decimal("0.001"), "t": Decimal(1000), "mg": Decimal("0.000001")}


def parse_mass_kg(text: str, fmt: str | None = None) -> Decimal:
    """A mass per piece in kg ("1250 g/Stück" -> 1.25). A bare number is taken as kg."""
    value = parse_number(text, fmt)
    unit = re.search(r"\d\s*(kg|mg|g|t)\b", _TAG.sub("", text), re.IGNORECASE)
    return value * _MASS_UNITS[unit.group(1).lower()] if unit else value


_WORKING_DAYS = re.compile(r"arbeitstag|werktag|working\s*days?|business\s*days?|work\s*days?",
                           re.IGNORECASE)


def parse_weeks(text: str) -> int:
    """
    A lead time in whole weeks ("3 Wochen", "3 weeks"). Days convert only when they make whole weeks: working days
    ("Arbeitstage", "Werktage", "business days") at 5 a week, calendar days at 7; anything else (months, a day count
    that is not whole weeks) raises, so the lead time stays unknown instead of wrong.
    """
    value = parse_number(text)
    if _WORKING_DAYS.search(text):
        per_week = 5
    elif re.search(r"tag|day", text, re.IGNORECASE):
        per_week = 7
    elif re.search(r"monat|month", text, re.IGNORECASE):
        raise ParseError(f"{text!r} is in months, not weeks")
    else:
        per_week = 1
    if value % per_week or value != value.to_integral_value():
        raise ParseError(f"{text!r} is not whole weeks")
    return int(value / per_week)


NONE_WORDS = re.compile(r"^(|-|—|–|keine?( gefordert| erforderlich| angegeben)?|ohne|none|n/?a|n\.\s?a|"
                        r"nicht gefordert)$", re.IGNORECASE)
# "No requirement" phrased in words: a negation followed by words only ("keine Zertifizierung", "ohne Zertifikat",
# "no certification", "without"), or a phrase saying it is not needed or does not apply ("nicht erforderlich",
# "not required", "entfällt"). A value with a digit is never taken as none.
_NEGATION = re.compile(r"^(keine?[nrms]?|ohne|no|none|without)(\s+[^\W\d_][^\s\d]*){0,3}$")
_NOT_NEEDED = re.compile(r"\b(nicht|not|kein|keine)\s+(erforderlich|gefordert|benötigt|benoetigt|notwendig|nötig|"
                         r"noetig|vorgeschrieben|zutreffend|anwendbar|required|needed|necessary|applicable|"
                         r"specified|requested)$|^(entfällt|entfaellt|unzutreffend|not applicable)$")


def norm_text(value: str | None) -> str:
    """Comparable form of a text attribute: case, spacing, ß, dashes; empty or "no requirement" wording becomes
    "none" ("-", "keine", "keine Zertifizierung", "ohne Zertifikat", "nicht erforderlich", "not required")."""
    t = " ".join(str(value or "").replace("ß", "ss").split()).strip(" .;").casefold()
    if NONE_WORDS.match(t):
        return "none"
    if not re.search(r"\d", t) and (_NEGATION.match(t) or _NOT_NEEDED.search(t)):
        return "none"
    return t


_CURRENCIES = {"€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "$": "USD", "us$": "USD", "usd": "USD",
               "us-dollar": "USD", "us dollar": "USD", "dollar": "USD", "dollars": "USD", "£": "GBP", "gbp": "GBP",
               "pfund": "GBP", "pound": "GBP", "pounds": "GBP", "britisches pfund": "GBP", "chf": "CHF",
               "sfr": "CHF", "franken": "CHF", "schweizer franken": "CHF", "swiss franc": "CHF",
               "swiss francs": "CHF", "¥": "JPY", "jpy": "JPY", "yen": "JPY", "yuan": "CNY", "renminbi": "CNY",
               "rmb": "CNY", "zloty": "PLN", "złoty": "PLN", "zł": "PLN"}


def parse_currency(text: str | None) -> str:
    """
    An ISO currency code from a page's wording: "€", "Euro" -> EUR; "$", "US-Dollar" -> USD; "£", "Schweizer
    Franken", ...; a code written in the text ("EUR (Euro)") is taken as is. '' when none is given; an unknown word
    comes back upper-cased in full (never cut to three letters, so it cannot pose as another code).
    """
    t = " ".join(str(text or "").split()).strip(" .:;()")
    if not t:
        return ""
    low = t.casefold()
    if low in _CURRENCIES:
        return _CURRENCIES[low]
    code = re.search(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])", t)
    if code:
        return code.group(0)
    for word, iso in sorted(_CURRENCIES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if re.search(rf"(?<![^\W\d_]){re.escape(word)}(?![^\W\d_])", low):
            return iso
    return t.upper()


REVISION_WORDS = {"revision", "rev", "index", "version", "ausgabe", "stand", "issue", "änderungsstand",
                  "aenderungsstand", "änderungsindex", "aenderungsindex"}


def _revision_tokens(text: str | None) -> list[str]:
    tokens = [t.strip(".-/") for t in re.findall(r"[\w./-]+", str(text or "").casefold())]
    return [t for t in tokens if t and t not in REVISION_WORDS]


def revision_id(text: str | None) -> str:
    """The identifier inside a revision label, comparable across sources: one source writes "Revision C of
    1 March", another just "C" or "Rev. C". The first token that is not a label word, without case."""
    return next(iter(_revision_tokens(text)), "")


def same_revision(a: str | None, b: str | None) -> bool:
    """Two labels of one revision: the same identifier, or a bare identifier that is a whole word of the other
    label. Whole words only: "A" is not inside "Revision B (aktuell)", "I" is not "III"."""
    ta, tb = _revision_tokens(a), _revision_tokens(b)
    if not ta or not tb:
        return False
    return ta[0] == tb[0] or (len(ta) == 1 and ta[0] in tb) or (len(tb) == 1 and tb[0] in ta)


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
    pieces_per_set: int | None = Field(default=None, description="After amendments; None = as the spec says")
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
                          scenario_date: date | datetime | None = None) -> EffectiveRequirement:
    """
    Hooks for the other workers' facts: scoped amendments from the tender Q&A (already filtered to the right sender
    by the caller; revision and requirement scoping and latest-wins happen here) and the per-end reserve values of
    the revision-specific Technikfreigabe, by reserve key. Without a needed reserve the stroke stays open.
    """
    base: dict[str, Decimal | str] = {"bore": req.bore, "stroke": req.stroke, "pressure": req.pressure,
                                      "mounting": req.mounting, "certification": req.certification}
    # An amendment names the revision its own way (label word, date or status around the identifier or not).
    scoped = [a.model_copy(update={"revision": req.revision}) if same_revision(a.revision, req.revision) else a
              for a in amendments or []]
    attrs = calc.apply_amendments(base, scoped, req.revision, req.requirement_id, scenario_date)
    pending: list[str] = []
    stroke: Decimal | None = Decimal(str(attrs["stroke"]))
    if req.stroke_is_usable:
        reserve = (reserves or {}).get(req.reserve_key or "")
        if reserve is None:
            pending.append(f"per-end reserve for key {req.reserve_key or '(none given)'}")
            stroke = None
        else:
            stroke = calc.nominal_stroke(stroke, Decimal(str(reserve)))
    pieces = req.pieces_per_set
    if "pieces_per_set" in attrs:  # an amended set size (rule 14) replaces the spec's for the Satz conversion
        try:
            pieces = parse_pieces_per_set(format(attrs["pieces_per_set"], "f")
                                          if isinstance(attrs["pieces_per_set"], Decimal) else attrs["pieces_per_set"])
        except ParseError as error:
            pending.append(f"amended pieces per set unreadable ({error})")
    return EffectiveRequirement(requirement_id=req.requirement_id, catalog_group=req.catalog_group,
                                bore=Decimal(str(attrs["bore"])), stroke=stroke,
                                pressure=Decimal(str(attrs["pressure"])), mounting=str(attrs["mounting"]),
                                certification=str(attrs["certification"]), pieces_per_set=pieces or None,
                                pending=pending)


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
        """The material for calc. A lead time not read stays None: calc fails only a position that needs it (stock
        or production, by pooled demand), and that position is omitted, never taken for a missing material."""
        return calc.Material(material_number=self.material_number, free_stock=self.free_stock or 0,
                             stock_lead_time_weeks=self.stock_lead_time_weeks,
                             production_lead_time_weeks=self.production_lead_time_weeks,
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
    no_hits: bool = Field(default=False, description="The catalogue group search was complete and empty: the site "
                                                     "itself said there are no hits (searched, no match)")


def group_without_hits(searches: list[dict], group: str) -> bool:
    """
    Whether a catalogue group's search is definitively empty, from the searches a worker ran (site_tools records
    query, rows and whether the site showed its own no-hits notice): no search listed any row, at least two
    searches showed the notice, and one of them searched for the group itself. A search that merely found nothing
    to read (a wrong field, a page that did not load) never counts.
    """
    key = calc.norm_code(group)
    if not key or not searches or any(s.get("rows") for s in searches):
        return False
    notices = [s for s in searches if s.get("no_hits")]
    return len(notices) >= 2 and any(key in calc.norm_code(s.get("query", "")) for s in notices)


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


TEXT_KEYS = ("mounting", "certification")


def wording_differences(req: EffectiveRequirement, attrs: dict[str, Decimal | str]) -> list[str]:
    """
    The text attributes in which a material differs from the requirement, when those are its only differences
    (every number equal, every attribute shown): the cases where only the wording may differ. [] otherwise.
    """
    want = req.attributes()
    if req.stroke is None or any(key not in attrs for key in want):
        return []
    differ = [key for key, value in want.items()
              if not calc.matching_materials({key: value}, [("x", {key: attrs[key]})], keys=(key,))]
    return differ if differ and all(key in TEXT_KEYS for key in differ) else []


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
                      material: MaterialRecord | None, pieces_per_set: int | None = None,
                      **extra) -> calc.PositionInput:
    """One position for calc: quantity in its stated unit (from the tender worker), pieces per set (an amended value
    from `effective_requirement` when given, else the spec's), the ERP material and its price conditions. `extra`
    passes the Q&A/mail facts (discount_scope, setup_fee, packaging, commitment) through."""
    return calc.PositionInput(
        position=position, quantity=quantity, unit=unit if isinstance(unit, calc.Unit) else calc.parse_unit(unit),
        pieces_per_set=pieces_per_set or requirement.pieces_per_set,
        material=material.to_material() if material else None,
        conditions=material.conditions if material else [], **extra)
