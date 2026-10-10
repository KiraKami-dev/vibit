"""
What the research workers bring back: one typed record per kind of fact, as the pages show it.

Values stay verbatim strings (a worker's result is checked against the pages it saw, so nothing may be reformatted
on the way). Each record says how its numbers and dates are written where the page states it; `parse.py` turns them
into numbers and times, and `bidplan.py` applies the bid rules through `calc.py`. A field the worker could not
establish is left empty: an empty field omits the position, it is never guessed.
"""
from typing import Literal

from pydantic import BaseModel, Field

NumberFormat = Literal["de", "en", ""]  # "1.234,56" or "1,234.56"; "" when the page does not say
DateOrder = Literal["dmy", "mdy", "ymd", ""]  # day-first, month-first or ISO; "" when the page does not say


class Dated(BaseModel):
    """When a record was issued. Prefer a machine-readable timestamp the page carries (e.g. datetime=...)."""
    date: str = Field(default="", description="Date or timestamp exactly as shown; the ISO one if the page has it")
    date_order: DateOrder = Field(default="", description="Order of day and month the page states for this date")
    timezone: str = Field(default="", description="Timezone or UTC offset the page states for it, as shown")


# ---------------------------------------------------------------- tender


class TenderPosition(BaseModel):
    position: str = Field(description="Position number exactly as the bill of quantities writes it")
    quantity: str = Field(description="Tender quantity as shown")
    unit: str = Field(description="Unit as shown (pieces or sets)")
    requirement: str = Field(default="", description="The specification requirement this position refers to")
    number_format: NumberFormat = ""


class QuantityChange(Dated):
    """A bidder Q&A answer that sets a new quantity for one position."""
    answer: str = Field(description="Number of the Q&A answer")
    position: str
    quantity: str
    unit: str = ""
    number_format: NumberFormat = ""


class Withdrawal(Dated):
    """A Q&A answer that withdraws an earlier answer."""
    answer: str = Field(description="Number of the withdrawing answer")
    withdraws: str = Field(description="Number of the answer it withdraws")


class Amendment(Dated):
    """A Q&A answer that changes named attributes of one requirement in one specification revision."""
    answer: str
    revision: str
    requirement: str
    attributes: dict[str, str] = Field(description="Only the attributes it names (bore, stroke, pressure, mounting, "
                                                   "certification), each value as shown")
    number_format: NumberFormat = ""


class TenderFindings(BaseModel):
    positions: list[TenderPosition] = []
    quantity_changes: list[QuantityChange] = []
    withdrawals: list[Withdrawal] = []
    amendments: list[Amendment] = []
    complete: bool = Field(default=False, description="True only if every position and every Q&A page was read")


# ---------------------------------------------------------------- specification


class Requirement(BaseModel):
    requirement: str = Field(description="Requirement identifier as the specification writes it")
    bore: str = ""
    stroke: str = ""
    pressure: str = ""
    mounting: str = ""
    certification: str = ""
    catalogue_group: str = Field(default="", description="The catalogue group named for this requirement")
    pieces_per_set: str = Field(default="", description="How many pieces make one set, if the requirement states it: that number alone, as shown, without the words around it")
    usable_stroke: bool = Field(default=False, description="True if the stroke is given as usable travel, to which "
                                                           "an end reserve must be added")
    reserve_key: str = Field(default="", description="The key naming the end reserve, if stated")
    number_format: NumberFormat = ""


class SpecFindings(BaseModel):
    current_revision: str = Field(default="", description="The revision the site marks as current")
    requirements: list[Requirement] = []
    complete: bool = Field(default=False, description="True only if every requirement page of that revision was read")


# ---------------------------------------------------------------- ERP


class Candidate(BaseModel):
    """One material the ERP lists for a catalogue group."""
    material_number: str
    bore: str = ""
    stroke: str = ""
    pressure: str = ""
    mounting: str = ""
    certification: str = ""
    status: str = Field(default="", description="Status exactly as shown")
    active: bool | None = Field(default=None, description="True if the status means usable, False if blocked")
    number_format: NumberFormat = ""


class Scale(BaseModel):
    min_quantity: str
    price: str


class Condition(BaseModel):
    """One price condition record of a material."""
    valid_from: str
    valid_to: str = ""
    date_order: DateOrder = ""
    currency: str = ""
    fx_key: str = ""
    price_unit: str = Field(default="", description="How many pieces the price refers to, as shown")
    scales: list[Scale] = []
    number_format: NumberFormat = ""


class MaterialFacts(BaseModel):
    """Stock, lead times and prices of the material chosen for a requirement."""
    material_number: str
    free_stock: str = Field(default="", description="Unrestricted stock, not blocked or reserved stock")
    stock_lead_time_weeks: str = ""
    production_lead_time_weeks: str = ""
    conditions: list[Condition] = []
    number_format: NumberFormat = ""


class ErpFindings(BaseModel):
    requirement: str
    candidates: list[Candidate] = []
    all_candidates_read: bool = Field(default=False, description="True only if the whole result list was read")
    chosen: str = Field(default="", description="Material number that matches the requirement exactly and is active; "
                                                "'' if none does")
    facts: MaterialFacts | None = None


# ---------------------------------------------------------------- mail


class MailRecord(Dated):
    reference: str = Field(description="The tender reference the message names, exactly as written")
    sender: str = Field(description="Sender address exactly as shown")
    subject: str = ""
    folder: str = ""


class Approval(MailRecord):
    """A discount approval message."""
    approves: bool = Field(description="True only if the message actually approves a discount")
    percent: str = ""
    min_order_value: str = Field(default="", description="Minimum net order value the approval requires, if any")
    number_format: NumberFormat = ""


class FxFixing(MailRecord):
    """An exchange-rate fixing message."""
    sent: bool = Field(default=True, description="False if the message is marked as a draft or not sent")
    rates: dict[str, str] = Field(default_factory=dict, description="FX key or currency -> EUR per unit, as shown")
    number_format: NumberFormat = ""


class TechnicalRelease(MailRecord):
    """An engineering release message that states the end reserve for requirements given as usable stroke."""
    revision: str = Field(default="", description="The specification revision the message names, as written")
    reserves: dict[str, str] = Field(default_factory=dict, description="Reserve key -> reserve per end, as shown")
    number_format: NumberFormat = ""


class MailFindings(BaseModel):
    mailbox_address: str = Field(default="", description="The signed-in mailbox's own address")
    approvals: list[Approval] = []
    fx_fixings: list[FxFixing] = []
    technical_releases: list[TechnicalRelease] = []
    complete: bool = Field(default=False, description="True only if inbox and archive results were both read")


class Findings(BaseModel):
    """Everything research established by the time the bid has to be entered; any part may be missing."""
    tender: TenderFindings = TenderFindings()
    spec: SpecFindings = SpecFindings()
    erp: dict[str, ErpFindings] = Field(default_factory=dict, description="By requirement identifier")
    mail: MailFindings = MailFindings()
