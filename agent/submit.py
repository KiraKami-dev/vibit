"""
Enter, sign and submit the bid: a worker on the bid website enters what the plan holds, following the brief's
submission steps. Before it signs, code compares what the form's summary shows with the plan (the read-back); the
signing TAN and, if research did not establish it, the contact address come from the bidder's webmail through
short lookups in tabs of their own.
"""
import asyncio
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable, Literal

from bidplan import BidPlan, PlanEntry
from briefing import Briefing
from browser_tools import Deps, SiteSession, unseen_values
from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from worker import run_worker

RESCUE_SECONDS = 150  # an entry still unfinished this long before the deadline is cut short: omit the rest, sign
CORRECTION_SECONDS = 240  # with less than this left, a read-back mismatch is signed as it is instead of corrected
STEPS_BASE, STEPS_PER_POSITION, STEPS_MAX = 60, 25, 260  # model requests the entry may use
# Thinking for the form-filling steps. The plan holds every decision, so the form is worked with thinking off (a
# step then answers in well under a second instead of several); AGENT_ENTRY_THINKING=1 turns it on to compare.
ENTRY_THINKING = os.environ.get("AGENT_ENTRY_THINKING", "0") == "1"
LOOKUP_SECONDS = 45  # one try of a mailbox lookup; a TAN is only valid for minutes, so a stalled try is given up
OPEN_SECONDS = 20  # opening the lookup's tab
LOAD_PAUSE_SECONDS = 0.6  # before a lookup's first view, for parts of a page that load after the page itself
ATTRIBUTES = ("bore", "stroke", "pressure", "mounting", "certification")

OpenTab = Callable[[], Awaitable[SiteSession]]


class MailboxAddress(BaseModel):
    address: str = Field(description="The email address of the mailbox you are signed in to")


class SigningTan(BaseModel):
    tan: str = Field(description="The TAN exactly as the message shows it")
    subject: str
    received: str = Field(description="When the message arrived, as shown")


class SubmissionResult(BaseModel):
    submitted: bool = Field(description="True only when the website confirmed the bid as submitted")
    confirmation: str = Field(description="The website's confirmation or error text, word for word")
    entered_positions: list[str] = Field(default=[], description="Position numbers submitted with values")
    omitted_positions: list[str] = Field(description="Position numbers the summary lists as omitted")


class SummaryLine(BaseModel):
    """One position as the form's summary shows it; '' for anything the summary does not show."""
    position: str = Field(description="Position number as shown")
    omitted: bool = Field(description="True if the summary shows the position as left out")
    material: str = Field(default="", description="Material or catalogue number as shown")
    quantity: str = Field(default="", description="Quantity as shown")
    unit_price: str = Field(default="", description="Net unit price as shown")
    base_unit_price: str = Field(default="", description="Base unit price as shown")
    lead_time: str = Field(default="", description="Lead time as shown")


class FormSummary(BaseModel):
    positions: list[SummaryLine] = []
    discount_percent: str = Field(default="", description="Discount as shown")
    total_net: str = Field(default="", description="Net total as shown (not a gross amount)")


@dataclass(frozen=True)
class Difference:
    """`extra`: the form holds a position the plan does not enter. `wrong`: a planned position shows another value.
    `missing`: a planned position is left out. `order`: discount or net total differ."""
    kind: Literal["extra", "wrong", "missing", "order"]
    position: str
    text: str


def plain(value: Decimal | int) -> str:
    """12.50: digits and a decimal point, no exponent."""
    return format(value, "f")


def german(value: Decimal | int) -> str:
    """12,50: decimal comma, no grouping (a form may read a grouping dot as the decimal point)."""
    return plain(value).replace(".", ",")


def enterable(entry: PlanEntry) -> bool:
    """A position is entered only with a material, a quantity and a price; anything less is left out, not guessed."""
    material = entry.material_number or any(getattr(entry, name) for name in ATTRIBUTES)
    return bool(not entry.omit and material and entry.quantity is not None and entry.unit_price is not None)


def plan_values(plan: BidPlan) -> str:
    """Every plan value the bid worker may enter or report, in the spellings a form may show. They come from other
    websites, so the worker's grounding check has to be told about them."""
    values = [plan.tender_reference]
    numbers: list[Decimal | int] = [plan.discount_percent, plan.total_net]
    for entry in plan.entries:
        values.append(entry.position)
        if enterable(entry):
            values += [entry.material_number, *(getattr(entry, name) for name in ATTRIBUTES)]
            numbers += [n for n in (entry.quantity, entry.lead_time_weeks, entry.unit_price, entry.base_unit_price)
                        if n is not None]
    for number in numbers:
        values += [plain(number), german(number), f"{number:,}", f"{number:,}".translate(str.maketrans(",.", ".,"))]
    return "\n".join(value for value in values if value)


def _entry_line(entry: PlanEntry) -> str:
    attributes = ", ".join(f"{name} {getattr(entry, name)}" for name in ATTRIBUTES if getattr(entry, name))
    parts = [f"position {entry.position}: material {entry.material_number or '(choose by its attributes)'}"
             + (f" ({attributes})" if attributes else ""), f"quantity {entry.quantity} pieces"]
    if entry.lead_time_weeks is not None:
        parts.append(f"lead time {entry.lead_time_weeks} weeks")
    parts.append(f"unit price {plain(entry.unit_price)} (German format {german(entry.unit_price)})")
    if entry.base_unit_price is not None:
        parts.append(f"base unit price {plain(entry.base_unit_price)} (German format {german(entry.base_unit_price)})")
    return "  - " + "; ".join(parts)


def bid_goal(plan: BidPlan, briefing: Briefing, contact_email: str = "") -> str:
    """The bid worker's goal: what to enter, taken from the plan, and how to get it through a form it has not seen."""
    contact = repr(contact_email) if contact_email else "the one bidder_contact_email() returns"
    order = (f"discount {plain(plan.discount_percent)} percent (German format {german(plan.discount_percent)}) and "
             f"net total {plain(plan.total_net)} (German format {german(plan.total_net)})")
    if plan.decision == "no_bid":
        decision = f"""- decision: no bid. Choose the form's option to decline or waive the bid, as the brief's submission
  steps describe it for no_bid, not an offer;
- enter no positions at all: no materials, quantities, prices or lead times, and never add positions;
- where the form still asks for them: {order};"""
    else:
        filled = [entry for entry in plan.entries if enterable(entry)]
        listed = [entry.position for entry in plan.entries if not enterable(entry)]
        if filled:
            enter = (f"- enter exactly these {len(filled)} positions, with exactly these values (they are decided; do "
                     f"not research or recalculate):\n" + "\n".join(_entry_line(entry) for entry in filled))
            leave = ("- leave out, with the form's explicit option to omit a position, "
                     + (f"positions {', '.join(listed)} and " if listed else "")
                     + "every other position the form shows that is not listed above; never add positions;")
        else:
            enter = ("- leave every position out with the form's explicit option to omit a position; do not configure "
                     "materials, prices or lead times, and never add positions;")
            leave = ""
        how = """- how to enter a position: the form may present the positions in any order and one per page, so read
  which position number a page is for before acting on it. Choose the material through the form's own choices so that
  every attribute equals the one given (choices may depend on each other and need confirming one at a time;
  options may be sorted as text; take the exact value, never a similar one) and it ends at the given material
  number. Quantity is in pieces. Type numbers in the format the form and the brief's steps ask for, without a
  currency sign or thousands separator. Enter the base unit price and the lead time only where the form has a
  field for them. Leave every other calculation or breakdown field (surcharges, packaging, bonus or rebate)
  empty: an empty field means "not answered", so type nothing into it, not even a zero;
- before leaving a position check that the form kept its values; if the form will not accept a position as given
  after two tries, omit that position instead and go on;
- going back clears the later steps, so do not go back while entering. If the session expires you are signed in
  again automatically: look at what the form kept and continue from there;"""
        decision = "\n".join(part for part in (
            "- decision: bid (the main offer, not an alternative offer);", enter, leave, how, f"- {order};") if part)
    return f"""Enter, sign and submit the bid for tender {plan.tender_reference} on this website, following the brief's
submission steps below. For this bid:
{decision}
- register with company name {briefing.company_name!r}, VAT id {briefing.vat_id!r} and contact email {contact};
- acknowledge the tender documents, but do not tick declarations that make your own terms and conditions apply;
- when the form shows its summary, and before you request the signing TAN, call report_summary() with what that
  page shows, copied as shown, and do what it answers;
- then request the signing TAN, call get_signing_tan() right away, enter that TAN and submit bindingly. Browser
  confirmation dialogs are accepted for you.
Return once the website confirms the submission (or shows why it failed).

Brief's submission steps:
{briefing.submission_steps}"""


def _key(position: str) -> str:
    """Position numbers compare by their digits: '010', 'Pos. 10' and '10' are the same position."""
    numbers = re.findall(r"\d+", position)
    return ".".join(str(int(n)) for n in numbers) if numbers else position.strip().casefold()


def _readings(text: str) -> set[Decimal]:
    """The numbers a shown value can mean: its first number read with a decimal comma and with a decimal point."""
    match = re.search(r"\d[\d.,]*", text)
    found: set[Decimal] = set()
    if match:
        token = match.group().rstrip(".,")
        for candidate in (token.replace(".", "").replace(",", "."), token.replace(",", "")):
            try:
                found.add(Decimal(candidate))
            except InvalidOperation:
                pass
    return found


def _differs(shown: str, expected: Decimal | int | None) -> bool:
    """True if the form shows a value and it is not the planned one. A field the summary does not show, or the plan
    does not have, is not compared."""
    return bool(shown.strip()) and expected is not None and Decimal(expected) not in _readings(shown)


def compare_summary(plan: BidPlan, summary: FormSummary) -> list[Difference]:
    """What the form's summary shows that the plan does not say, and the other way round."""
    wanted = {_key(e.position): e for e in plan.entries if enterable(e)} if plan.decision == "bid" else {}
    found: list[Difference] = []
    shown = set()
    for line in summary.positions:
        key = _key(line.position)
        shown.add(key)
        entry = wanted.get(key)
        if line.omitted:
            if entry:
                found.append(Difference("missing", line.position, f"position {line.position} is left out, but the "
                                                                  f"bid enters it"))
            continue
        if entry is None:
            found.append(Difference("extra", line.position, f"position {line.position} holds values, but the bid "
                                                            f"leaves it out"))
            continue
        number = "".join(entry.material_number.split()).casefold()
        checks = [("material", bool(line.material.strip() and number)
                   and number not in "".join(line.material.split()).casefold(), entry.material_number),
                  ("quantity", _differs(line.quantity, entry.quantity), entry.quantity),
                  ("unit price", _differs(line.unit_price, entry.unit_price), entry.unit_price),
                  ("base unit price", _differs(line.base_unit_price, entry.base_unit_price), entry.base_unit_price),
                  ("lead time", _differs(line.lead_time, entry.lead_time_weeks), entry.lead_time_weeks)]
        shows = {"material": line.material, "quantity": line.quantity, "unit price": line.unit_price,
                 "base unit price": line.base_unit_price, "lead time": line.lead_time}
        for name, differs, expected in checks:
            if differs:
                found.append(Difference("wrong", line.position, f"position {line.position}: {name} shows "
                                                                f"{shows[name]!r}, the bid says {expected}"))
    if summary.positions:  # a summary that lists no positions at all cannot be compared position by position
        for key, entry in wanted.items():
            if key not in shown:
                found.append(Difference("missing", entry.position, f"position {entry.position} is not in the "
                                                                   f"summary, but the bid enters it"))
    if _differs(summary.discount_percent, plan.discount_percent):
        found.append(Difference("order", "", f"discount shows {summary.discount_percent!r}, the bid says "
                                             f"{plain(plan.discount_percent)}"))
    if _differs(summary.total_net, plan.total_net):
        found.append(Difference("order", "", f"net total shows {summary.total_net!r}, the bid says "
                                             f"{plain(plan.total_net)}"))
    return found


class ReadBack:
    """The check between entering and signing. The worker reports the summary through a tool instead of ending a
    first stage with a typed result: that is one model request, and a correction continues in the same conversation,
    where a second stage would have to find its way around the form again.

    A mismatch is sent back once with exactly what differs. If the summary still differs afterwards, the bid is
    signed as it stands when the form merely holds less than the plan (a planned position left out, or a discount or
    total that no omission could repair): a signed partial bid keeps its other points. A position whose values the
    plan does not contain (a wrong value, or a position the plan omits) is omitted instead: omitted and wrong score
    the same for that position, but a wrong line also puts a figure in a signed bid that nobody calculated.
    """

    def __init__(self, plan: BidPlan, session: SiteSession, deadline: float, corrections: int = 1):
        self.plan, self.session, self.deadline, self.corrections = plan, session, deadline, corrections
        self.done = False  # True once the worker has been told to request the TAN
        self.refused = self.reminded = 0

    def check(self, summary: FormSummary) -> str:
        """Compare a reported summary with the plan and say what the worker does next."""
        not_shown = unseen_values(summary.model_dump_json(), self.session.view)
        if not_shown and self.refused < 2:  # the report has to be read from the page, not repeated from the goal
            self.refused += 1
            self.session.trace.event(self.session.name, "summary_refused", missing=not_shown[:10])
            return (f"{', '.join(not_shown[:10])} are not on the page you are on. Open the form's summary and report "
                    f"only what it shows, spelled as shown; leave a field '' if the summary does not show it.")
        differences = compare_summary(self.plan, summary)
        left = self.deadline - time.monotonic()
        self.session.trace.event(self.session.name, "read_back", positions=len(summary.positions),
                                 differences=[d.text for d in differences], corrections_left=self.corrections)
        if not differences:
            self.done = True
            return "The summary matches the bid. Request the signing TAN now and call get_signing_tan()."
        listed = "; ".join(d.text for d in differences)
        if self.corrections > 0 and left > CORRECTION_SECONDS:
            self.corrections -= 1
            return (f"The summary differs from the bid: {listed}. Correct exactly this with the form's own controls "
                    f"(if that means going back, enter the cleared steps again as the goal says), then call "
                    f"report_summary() again from the summary.")
        self.done = True
        unplanned = sorted({d.position for d in differences if d.kind in ("extra", "wrong")})
        if unplanned and left > CORRECTION_SECONDS:
            return (f"The summary still differs: {listed}. Leave out position {', '.join(unplanned)} with the form's "
                    f"option to omit a position and keep everything else as it is. Then request the signing TAN, "
                    f"call get_signing_tan() and submit; do not report again.")
        return (f"The summary still differs ({listed}), but sign it as it is: request the signing TAN now and call "
                f"get_signing_tan().")


async def close_tab(session: SiteSession | None) -> None:
    for page in list(session.pages) if session else []:
        try:
            await asyncio.wait_for(page.close(), 5)
        except Exception:
            pass


async def let_finish(session: SiteSession, rounds: int = 4) -> str:
    """Let a page that fills in step by step finish (a pane that loads once a check is passed, a text that loads
    once the pane is there): pause and settle until two views in a row are the same. Returns the view."""
    before = None
    for _ in range(rounds):
        await asyncio.sleep(LOAD_PAUSE_SECONDS)
        await asyncio.wait_for(session.settle(), OPEN_SECONDS)
        await session.look()
        now = re.sub(r"\[e\d+", "[e", session.view)  # refs are renumbered with every view
        if now == before:
            break
        before = now
    return await session.look()


class Mailbox:
    """The bidder's webmail for the bid's own lookups (contact address, signing TAN).

    Every lookup starts from a known state: a tab of its own at the mailbox's start page, freshly loaded and left
    to finish loading before the worker gets its first view, and closed afterwards. Traces of failed TAN lookups
    showed three causes. A lookup that begins where an earlier one stopped (an opened message, a filtered list)
    spends its requests finding the way back. A view taken while the page is still filling in shows the newest
    message selected but its text missing; the worker then tries other controls to open it, some of which lead
    away from the mailbox or stall the tab. And opening and signing in to a tab can itself take many seconds, which
    the bid form's session and the TAN do not have. So the next lookup's tab is opened ahead of time (`prepare`)
    and only reloaded when the lookup starts, the page is left to finish (`let_finish`) before the first view, and
    each try has a time limit: a stalled tab costs one try, not the signing.
    """

    def __init__(self, open_tab: OpenTab, briefing: Briefing, deadline: float):
        self.open_tab, self.briefing, self.deadline = open_tab, briefing, deadline
        self.last_tan = ""
        self.repeated = False
        self.ready: asyncio.Task | None = None  # a signed-in tab being opened for the next lookup

    def prepare(self) -> None:
        """Start opening the next lookup's tab now, so the lookup itself only has to reload it."""
        if self.ready is None:
            self.ready = asyncio.create_task(self.open_tab())

    async def close(self) -> None:
        """Give up the prepared tab: the bid is through."""
        ready, self.ready = self.ready, None
        if ready and not ready.done():
            ready.cancel()
        elif ready and not ready.cancelled() and not ready.exception():
            await close_tab(ready.result())

    async def _tab(self) -> SiteSession:
        """The prepared tab, reloaded so it shows the mailbox as it is now; else a new one."""
        ready, self.ready = self.ready, None
        if ready:
            session = None
            try:
                session = await asyncio.wait_for(ready, OPEN_SECONDS)
                await asyncio.wait_for(session.page.goto(session.site["url"], wait_until="domcontentloaded"),
                                       OPEN_SECONDS)
                await asyncio.wait_for(session.settle(), OPEN_SECONDS)
                return session
            except Exception:
                await close_tab(session)
        return await asyncio.wait_for(self.open_tab(), OPEN_SECONDS)

    async def _lookup(self, goal: str, output_type, max_steps: int):
        for _ in range(2):  # a tab that does not open or stalls is replaced once
            deadline = min(self.deadline, time.monotonic() + LOOKUP_SECONDS)
            session = None
            try:
                session = await self._tab()
                await let_finish(session)

                async def wait_for_page(ctx: RunContext[Deps]) -> str:
                    """Wait for the current page to finish loading and show it again. Use it when something you
                    just opened shows no content yet."""
                    return await let_finish(ctx.deps.session)

                found, _ = await run_worker(session, goal, output_type, deadline=deadline, max_steps=max_steps,
                                            notes=False, tools=(wait_for_page,))
                if found:
                    return found
            except asyncio.TimeoutError:
                pass
            finally:
                await close_tab(session)
        return None

    async def address(self) -> str:
        found = await self._lookup(
            "Find the email address of the mailbox you are signed in to (the bidder's own address, usually shown "
            "as the signed-in account). Return it at once.", MailboxAddress, 8)
        return found.address if found else ""

    async def tan(self) -> SigningTan | None:
        """The newest signing TAN. The sites run on the scenario's clock, so "newest" is the highest request number
        or the top of the list, never a wall-clock time."""
        older = (f" The TAN {self.last_tan} was returned before: a newer message should be listed above it. If "
                 f"none is, return the newest one there is.") if self.last_tan else ""
        found = await self._lookup(
            f"A signing TAN for tender {self.briefing.tender_reference} was requested a moment ago and its message "
            "has just arrived in this mailbox. You are on the mailbox's start page: new messages are listed there "
            "without any search, newest first. Find the newest signing TAN message for exactly this tender "
            "reference (if several are listed, the one with the highest request number). If its text with the TAN "
            "is already shown, return the TAN at once; otherwise open that message by its title. If an opened "
            "message shows no text yet, call wait_for_page() instead of opening it through other controls. Do "
            f"not search and do not open other messages.{older}", SigningTan, 10)
        self.prepare()  # a TAN can be refused or run out: have the next lookup's tab ready
        if found:
            self.repeated = found.tan == self.last_tan  # no newer message: the website was not asked for a new TAN
            self.last_tan = found.tan
        return found


async def submit_bid(bid_session: SiteSession, mailbox: Mailbox, briefing: Briefing, plan: BidPlan, deadline: float,
                     contact_email: str = "") -> SubmissionResult | None:
    """Enter the plan in the bid form, check the form's summary against it, sign with a TAN and submit.

    `deadline` is the `time.monotonic()` value by which the bid has to be signed. An entry that is not through
    `RESCUE_SECONDS` before it is cut short: a second worker omits what is unfinished and signs.
    """
    address = None if contact_email else asyncio.create_task(mailbox.address())
    # values from the other websites and from the calculation; let the worker enter and report them
    bid_session.seen += "\n" + plan_values(plan) + "\n" + contact_email
    read_back = ReadBack(plan, bid_session, deadline)
    mailbox.prepare()
    tans: list[str] = []  # the TANs handed to the worker; with one out, the bid may already be submitted

    async def bidder_contact_email(reason: str = "") -> str:
        """The bidder's contact email address, read from the bidder's own webmail."""
        nonlocal address
        found = contact_email or await address
        if not found:  # the lookup failed earlier; an answer of "not found" would only be asked for again
            address = asyncio.create_task(mailbox.address())
            found = await address
        bid_session.seen += "\n" + found
        return found or "not found: the webmail did not show it"

    async def report_summary(summary: FormSummary) -> str:
        """Call on the form's summary page before requesting the signing TAN: report every position the summary
        lists, and the discount and net total, exactly as that page shows them. The answer says what to do next."""
        return read_back.check(summary)

    async def get_signing_tan(reason: str = "") -> str:
        """Call right after requesting the signing TAN on this website: returns the newest TAN for this tender
        from the bidder's webmail. A TAN is valid for five minutes and any new request invalidates it."""
        if not read_back.done and read_back.reminded < 1:  # asked once; a signed bid matters more than the check
            read_back.reminded += 1
            return ("First call report_summary() with what the form's summary shows. If you have already requested "
                    "a TAN on the website, request a new one after that and call this again.")
        found = await mailbox.tan()
        if not found:
            return "No TAN found in the webmail. Request a new TAN and call this again."
        bid_session.seen += "\n" + found.tan
        tans.append(found.tan)
        if mailbox.repeated:
            return (f"TAN {found.tan} is still the newest in the webmail: the same one as before. If the website "
                    f"refused it, request a new TAN on the website first, then call this again.")
        return f"TAN {found.tan} (message {found.subject!r}, received {found.received})"

    tools = (report_summary, get_signing_tan) if contact_email else (
        bidder_contact_email, report_summary, get_signing_tan)
    filled = sum(enterable(entry) for entry in plan.entries) if plan.decision == "bid" else 0
    steps = min(STEPS_BASE + STEPS_PER_POSITION * filled, STEPS_MAX)
    entry_deadline = deadline - RESCUE_SECONDS if filled else deadline
    try:
        result, _ = await run_worker(bid_session, bid_goal(plan, briefing, contact_email), SubmissionResult,
                                     deadline=entry_deadline, max_steps=steps, thinking=ENTRY_THINKING, tools=tools)
        if (result is None or not result.submitted) and deadline - time.monotonic() > 30:
            # Out of time or steps, or the form refused: what the form holds stays, the rest is left out, and it
            # is signed. The read-back then only stops values the plan does not contain; nothing is corrected.
            bid_session.trace.event(bid_session.name, "rescue", seconds_left=round(deadline - time.monotonic()))
            read_back.corrections = 0
            read_back.done = False
            rescue = (f"The bid form on this website may be partly filled, and little time is left. Finish it.\n"
                      f"{bid_goal(plan, briefing, contact_email)}\n\n"
                      f"Because little time is left: keep what the form already holds, do not enter any further "
                      f"position, leave out every position that is not completely entered yet, and go straight on "
                      f"to the summary, the signing TAN and the binding submission. If the website shows that the "
                      f"bid has already been submitted, return that.")
            result, _ = await run_worker(bid_session, rescue, SubmissionResult, deadline=deadline,
                                         max_steps=STEPS_BASE, thinking=ENTRY_THINKING, tools=tools)
    except Exception as error:
        if not tans:
            raise
        # A submitted bid ends the run, and with it the run's access to the model: the request that would have
        # reported the confirmation fails. With a TAN handed out that is the expected end, not a failure, and
        # nothing may be tried again (every further TAN request or model call would be refused the same way).
        bid_session.trace.event(bid_session.name, "run_over", reason=f"{type(error).__name__}: {str(error)[:200]}",
                                status=getattr(error, "status_code", None))
        result = SubmissionResult(submitted=getattr(error, "status_code", None) in (401, 403),
                                  confirmation="the model was no longer reachable after the signing TAN was entered",
                                  omitted_positions=[])
    finally:
        if address and not address.done():
            address.cancel()
        await mailbox.close()
    return result
