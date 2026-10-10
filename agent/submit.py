"""
Enter, sign and submit the bid: a worker on the bid website follows the brief's submission steps, and asks the
bidder's webmail (a second worker in its own tab) for the contact address and, once requested, the signing TAN.

The bid worker runs in two phases: filling the form up to the signing step (plain navigation, thinking off by
default), then requesting the TAN, entering it and submitting. Between them one model call checks the
summary against the requirements; the signing phase thinks only when that check fails or finds problems.
THINKING_FORM, THINKING_REVIEW (the check) and THINKING_SIGN override these defaults (see llm.thinking).
"""
import asyncio
import os
import time
from decimal import Decimal
from typing import Literal

import browser_tools
import llm
from briefing import Briefing
from browser_tools import Deps, SiteSession
from calc import position_key
from configurator import FillPlan, complete_positions, fill_position, find_control, german, position_on_page
from extract import locate
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from worker import run_worker

ADDRESS_WAIT = 15  # seconds the bid worker waits for the contact address before it starts without it
# seconds kept for the review, the TAN round trip and submitting (v3: 180, was 240; measured 26 to 115 s)
SIGNING_RESERVE = float(os.environ.get("AGENT_SIGNING_RESERVE", "180"))
LAST_CHANCE = 45  # seconds a final sign-only pass needs at least
# AGENT_FILL_IN=0 leaves out fill_in(), the several-actions-per-step form tool


class MailboxAddress(BaseModel):
    address: str = Field(description="The email address of the mailbox you are signed in to")


class SigningTan(BaseModel):
    tan: str = Field(description="The TAN exactly as the message shows it")
    subject: str
    received: str = Field(description="When the message arrived, as shown")


class FormReady(BaseModel):
    at_signing_step: bool = Field(description="True when the page now shows the final review with the signing step")
    open_points: str = Field(description="Anything you could not enter or that the page still asks for; '' if none")


class SubmissionResult(BaseModel):
    submitted: bool = Field(description="True only when the website confirmed the bid as submitted")
    confirmation: str = Field(description="The website's confirmation or error text, word for word")
    omitted_positions: list[str] = Field(description="Position numbers the summary lists as omitted")


class Mailbox:
    """The bidder's webmail, worked by the model on request; one request at a time in its tab."""

    def __init__(self, session: SiteSession, briefing: Briefing, deadline: float):
        self.session, self.briefing, self.deadline = session, briefing, deadline
        self.lock = asyncio.Lock()
        self.last_tan = ""

    async def address(self) -> str:
        """The bidder's own address, or "" when the webmail cannot tell: never an error that would stop the bid."""
        try:
            async with self.lock:
                found, _ = await run_worker(
                    self.session, "Find the email address of the mailbox you are signed in to (the bidder's own "
                    "address, usually shown as the signed-in account). Return it at once.",
                    MailboxAddress, deadline=self.deadline, max_steps=8, notes=False)
        except Exception as error:
            self.session.trace.event(self.session.name, "stopped", reason=f"address: {type(error).__name__}: {error}")
            return ""
        return found.address if found else ""

    async def tan(self, answer: str = "") -> SigningTan | None:
        """
        The signing TAN just requested. The sites run on the scenario's clock, so "newest" is the highest request
        number or the top of the list, never a wall-clock time. `answer` is what the bid website said to the request
        (it may name the request), so the mail worker can pick that very message.
        """
        older = f" The TAN {self.last_tan} you returned before is no longer valid." if self.last_tan else ""
        said = (f" The bid website answered the request with: {answer!r}; pick the message for that request."
                if answer else "")
        try:
            return await self._tan(older, said, answer)
        except Exception as error:  # the bid worker asks again; an error here must not stop the bid
            self.session.trace.event(self.session.name, "stopped", reason=f"tan: {type(error).__name__}: {error}")
            return None

    async def _tan(self, older: str, said: str, answer: str) -> SigningTan | None:
        async with self.lock:
            async with self.session.lock:  # the list the tab shows predates the request: load it again
                await self.session.revive()
                await self.session._goto(self.session.page.url)
            found, _ = await run_worker(
                self.session, f"A new signing TAN for tender {self.briefing.tender_reference} was just requested."
                f"{said} Open the newest signing TAN message for exactly this tender reference (the highest "
                "request number; new TAN messages are listed without a search, newest first; reload the list once "
                "if it is not there yet), copy the TAN from its text and return it at once. If a preview pane "
                f"stays empty, open the message's own page through its link.{older}",
                SigningTan, context=answer, deadline=self.deadline, max_steps=10, notes=False)
        if found:
            self.last_tan = found.tan
        return found


class FormAction(BaseModel):
    ref: str = Field(description="The element's ref in the latest view")
    action: Literal["fill", "select", "check", "uncheck", "click"]
    value: str = Field("", description="fill: the text to type; select: the option's visible text, exactly as the "
                                       "view lists it; empty for check, uncheck and click")


async def _perform(element, step: FormAction) -> None:
    if step.action == "fill":
        await element.fill(step.value, timeout=2000)
    elif step.action == "select":
        if not step.value:
            raise ValueError("select needs the option's visible text as value")
        try:
            await element.select_option(label=step.value, timeout=2000)
        except Exception:
            await element.select_option(step.value, timeout=2000)
    elif step.action in ("check", "uncheck"):
        await element.set_checked(step.action == "check", timeout=2000)
    else:
        try:
            await element.click(timeout=5000)
        except Exception:
            await element.evaluate("e => e.click()")


async def fill_in(ctx: RunContext[Deps], actions: list[FormAction]) -> str:
    """
    Do several actions on the current page in one go, in this order: fill textboxes, choose options, tick boxes,
    or click buttons that keep you on this page (such as one per table row). Use refs from the latest view. It
    stops at the first action that fails or that leaves the page, and returns the new view. Put a button that
    moves to the next step only last, or click it on its own.
    """
    session = ctx.deps.session
    done: list[str] = []
    stop = ""
    async with session.lock:
        page, url, windows = session.page, session.page.url, len(session.pages)
        for step in actions:
            element = await locate(page, step.ref)
            if element is None:
                stop = f"no element {step.ref} (the page changed; use the new view)"
                break
            try:
                await _perform(element, step)
            except Exception as error:
                stop = f"{step.action} {step.ref} {step.value!r} failed: {str(error).splitlines()[0][:200]}"
                break
            done.append(f"{step.action} {step.ref}")
            await asyncio.sleep(0.15)
            if page.is_closed() or page.url != url or len(session.pages) != windows:
                if step is not actions[-1]:
                    stop = "the page changed after it; the remaining actions were not done"
                break
        await asyncio.sleep(0.25)
        await session.settle()
        session.trace.event(session.name, "action", action="fill_in", done=done, stop=stop,
                            asked=[step.model_dump() for step in actions])
        view = await session.look()
    report = f"Done: {', '.join(done) or 'nothing'}." + (f" Stopped: {stop}." if stop else "")
    return f"{report}\n\n{view}"


class PositionDesk:
    """
    The bid worker's tools for the positions: enter the calculated values of the position the model says is on
    screen, and total what was entered. Positions that fail are left for the model to omit.
    """

    def __init__(self, entries: dict[str, dict], deadline: float):
        self.entries, self.deadline = entries, deadline
        self.plan = FillPlan()
        self.failures: dict[str, int] = {}
        self.entered: set[str] = set()
        self.choices: dict[str, dict[str, str]] = {}
        self.next_button = ""

    @property
    def tools(self) -> tuple:
        return (self.configure_position, self.net_total) if self.entries else ()

    async def configure_position(self, ctx: RunContext[Deps], position: str, fields: dict[str, str] | None = None,
                                 confirm_button: str = "", next_button: str = "",
                                 choices: dict[str, str] | None = None) -> str:
        """
        Enter all values of the position the form shows now and, once all are in, save it and go on with the form's
        next/continue button. `position`: its number as the page shows it. `fields`: control label (exactly as in
        your view) -> value name; needed on the first call, later calls reuse it. `confirm_button`: label of the
        button that applies a choice so the next choice opens. `next_button`: label of the button that saves the
        position and goes on (never the one that omits or adds a position); needed once. `choices`: only when a
        call reported that no option equals an attribute: control label -> the option that means the same.
        """
        session, plan, position = ctx.deps.session, self.plan, position.strip()
        known = set().union(*self.entries.values()) - {"position"}
        if fields:  # later calls add to or correct the mapping, they never drop what was mapped before
            given = {label: name.strip() for label, name in fields.items()}
            unknown = sorted({name for name in given.values() if name not in known})
            if unknown:
                return f"Unknown value names {unknown}; use only: {', '.join(sorted(known))}. Call again."
            plan.fields = {label: name for label, name in plan.fields.items() if name not in given.values()}
            plan.fields.update(given)
        if confirm_button:
            plan.confirm = confirm_button
        if next_button:
            self.next_button = next_button
        entry = self.entries.get(position)
        if entry is None:
            return (f"No values for position {position!r}: leave it out with the form's option to omit a position. "
                    + self.so_far(session))
        if not plan.fields:
            return "Give fields (control label -> value name) for the controls of this form first."
        if self.failures.get(position, 0) >= 3:
            return (f"Position {position} failed three times: leave it out with the form's option to omit a "
                    f"position. {self.so_far(session)}")
        if time.monotonic() > self.deadline - SIGNING_RESERVE:
            return ("Time is short: leave this and every remaining position out and go on to signing. "
                    + self.so_far(session))
        async with session.lock:
            if not position_on_page(await session.look(), position):
                return (f"Position {position} is not on the page now. Configure the position the page shows, by "
                        "its own number.")
            log: list[str] = []
            try:
                remembered = self.choices.setdefault(position, {})
                remembered.update(choices or {})  # a revisit of the position needs the same picks
                error = await fill_position(session, entry, plan, log, remembered)
            except Exception as exc:  # a slow or changed page: the model decides to retry or omit
                error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
            moved = ""
            if not error and self.next_button:  # the model named the button; clicking it is the mechanical part
                view = await session.look()
                ref, why = find_control(view, self.next_button, ("button",))
                target = await locate(session.page, ref) if ref else None
                if target is None:
                    moved = f" Could not save it: {why}; click the form's next/continue button yourself."
                else:
                    await target.click(timeout=5000)
                    await session.settle()
                    moved = f" Saved with {self.next_button!r}; the page below is the form's next step."
            session.trace.event(session.name, "configure_position", position=position, done=log, error=error,
                                moved=moved)
            view = await session.look()
        if error:
            self.failures[position] = self.failures.get(position, 0) + 1
            self.entered.discard(position)
            return (f"Position {position} not fully entered: {error}. Entered so far: {'; '.join(log) or 'nothing'}. "
                    "Fix the mapping and call again, or leave the position out with the form's option to omit "
                    f"it. {self.so_far(session)}\n\n{view}")
        self.entered.add(position)
        then = moved or " Check it, then save and continue with the form's next/continue button."
        return f"Position {position} entered: {'; '.join(log)}.{then} {self.so_far(session)}\n\n{view}"

    def so_far(self, session: SiteSession) -> str:
        """The running net total, told after every position so the totals step needs no arithmetic."""
        total = german(self.total())
        session.seen += "\n" + total  # our own figure: let the worker's result mention it
        entered = ", ".join(sorted(self.entered, key=position_key)) or "none"
        return f"Net total of the positions entered so far: {total} ({entered}); enter the latest such figure."

    def entry_total(self, position: str) -> Decimal:
        entry = self.entries[position]
        return Decimal(str(entry["quantity"])) * Decimal(str(entry["unit_price"]))

    def total(self, omitted: set[str] = frozenset()) -> Decimal:
        return sum((self.entry_total(p) for p in self.entered - omitted), Decimal(0))

    def expected(self) -> str:
        """What the review should show for each position entered, for the summary check and the signing pass."""
        lines = []
        for p in sorted(self.entered, key=position_key):
            e = self.entries[p]
            extra = ", ".join(f"{label} {german(e[key])}" for key, label in (
                ("base_unit_price", "base price"), ("surcharge_per_unit", "surcharge per piece"),
                ("packaging_per_unit", "packaging per piece"), ("rebate_total", "bonus for the position"))
                if e.get(key) is not None)
            lines.append(f"  - position {p}: material {e['material_number']}, quantity {e['quantity']}, net unit "
                         f"price {german(e['unit_price'])}, lead time {e['lead_time_weeks']} weeks"
                         + (f", {extra}" if extra else ""))
        return "\n".join(lines)

    async def net_total(self, omitted: list[str] | None = None) -> str:
        """
        The bid's net total in German format: quantity x unit price summed over the positions entered with
        configure_position(), minus any listed in `omitted` (positions you left out after all).
        """
        dropped = {p.strip() for p in omitted or []}
        return f"{german(self.total(dropped))} (positions {', '.join(sorted(self.entered - dropped)) or 'none'})"


def _positions(answer: dict | None, desk: PositionDesk, filled: bool) -> str:
    """
    The decision and position part of the requirements: before the form phase, how to enter them; after it, what
    the review must show (the positions actually entered, all others omitted, the total of the entered ones).
    """
    discount = german((answer or {}).get("discount_percent") or 0)
    if answer and answer.get("decision") == "no_bid":
        return ("- decision: no bid: choose the form's declaration that you do not submit an offer (not a main or "
                f"alternative offer); no positions;\n- discount {discount} and net total 0,00;")
    head = "- decision: bid (main offer, not an alternative offer);\n"
    if filled and desk.entered:
        return head + (f"- exactly {len(desk.entered)} position(s) entered, each with these values:\n"
                       f"{desk.expected()}\n"
                       "- every other position omitted, and no added or extra position;\n"
                       f"- discount {discount} and net total {german(desk.total())} (German number format; never a "
                       "gross amount);")
    if not desk.entries:
        return head + ("- leave every position out with the form's option to omit a position (where the form shows "
                       "one position per step, omit each in turn); do not configure materials, prices or lead times, "
                       f"and never use options that add positions;\n- discount {discount} and net total 0,00 (German "
                       "number format);")
    names = sorted({key for entry in desk.entries.values() for key in entry} - {"position"})
    return head + f"""- enter exactly these positions: {', '.join(sorted(desk.entries))}. The form shows one position
  at a time, in its own order. On every position page, first call configure_position() with the number the page
  shows: it enters all values of a position of this bid and saves it with the next/continue button you name, or
  tells you to omit it. Never pick options or type position values yourself;
- configure_position() needs, once, which control takes which value: map each control's label (as your view shows
  it) to one of these value names: {', '.join(names)}. Choices that open further choices (attributes of the
  material) map to the attribute with the same meaning; the material/catalogue number control to material_number;
  unit price, base price, surcharge, packaging and the bonus or rebate for the whole position to unit_price,
  base_unit_price, surcharge_per_unit, packaging_per_unit and rebate_total; lead time to lead_time_weeks. Leave out
  controls with no matching value. Also name the button that applies a choice so the next one opens, and the
  button that saves the position and goes on to the next step (not the ones that omit or add a position);
- leave out, with the form's option to omit a position, every other position and any position where
  configure_position() reports a problem it cannot resolve. Never use options that add positions; if the form
  shows an extra position you did not ask for, remove it or leave it out;
- discount {discount}; net total: the latest figure configure_position() reports for the positions entered so
  far (net_total() repeats it; pass positions you entered but left out after all). Enter both exactly as given
  (German number format), never a sum you work out yourself, a gross amount or a sum the page proposes;"""


def _requirements(briefing: Briefing, email: str, answer: dict | None = None, desk: PositionDesk | None = None,
                  filled: bool = False) -> str:
    contact = repr(email) if email else "the contact email from bidder_contact_email()"
    return f"""{_positions(answer, desk or PositionDesk({}, 0), filled)}
- register with company name {briefing.company_name!r}, VAT id {briefing.vat_id!r} and contact email {contact};
- tick the form's acknowledgement of the tender documents (no need to open them), but do not tick declarations
  that make your own terms and conditions apply."""


class SummaryCheck(BaseModel):
    matches: bool = Field(description="True when the page contradicts none of the requirements")
    problems: str = Field(description="Each requirement the page visibly contradicts, with what the page shows; "
                                      "'' if none")


async def check_summary(view: str, requirements: str) -> SummaryCheck | None:
    """One model call (thinking off by default, THINKING_REVIEW) on the review page before signing."""
    agent = Agent(llm.model(), output_type=SummaryCheck, retries=2,
                  model_settings=llm.settings(llm.thinking("review", False)),
                  instructions="You check the final review page of a bid against the bidder's requirements before "
                               "it is signed. Report only what the page visibly contradicts (a wrong or missing "
                               "value, a position that is not omitted, an extra position, a wrong offer type); "
                               "requirements the page does not show at all are no problem.")
    try:
        result = await asyncio.wait_for(agent.run(f"Requirements:\n{requirements}\n\nReview page:\n{view}"), 90)
    except Exception:  # a failed check means the signing worker checks the page itself, with thinking on
        return None
    return result.output


async def submit_bid(bid_session: SiteSession, mailbox: Mailbox, briefing: Briefing, deadline: float,
                     answer: dict | None = None, attributes: dict[str, dict[str, str]] | None = None
                     ) -> SubmissionResult | None:
    """
    Enter the calculated `answer` (calc's BidResult.answer()), sign and submit it. Positions the answer does not
    give completely, or that fail to enter, are omitted; without an answer every position is omitted (discount 0,
    total 0). `attributes` holds per position number the chosen material's attributes (e.g. its dimensions), which
    the form's choices lead through to the catalogue number.
    """
    desk = PositionDesk(complete_positions(answer or {}, attributes), deadline)
    address = asyncio.create_task(mailbox.address())
    try:  # the lookup takes a step or two; knowing the address up front saves the bid worker a round trip
        email = await asyncio.wait_for(asyncio.shield(address), ADDRESS_WAIT)
    except asyncio.TimeoutError:
        email = ""
    if email:
        bid_session.seen += "\n" + email  # it comes from another website; let results use it

    async def bidder_contact_email(reason: str = "") -> str:
        """The bidder's contact email address, read from the bidder's own webmail."""
        found = await address
        bid_session.seen += "\n" + found
        return found or "not found: the webmail did not show it"

    async def request_signing_tan(ctx: RunContext[Deps], ref: str) -> str:
        """
        Click the website's control that requests the signing TAN (its ref in the latest view), then fetch that
        TAN from the bidder's webmail. Returns the page after the request and the TAN: enter it right away (it is
        valid for five minutes, and any new request invalidates it).
        """
        before = set(bid_session.view.splitlines())
        view = await browser_tools.click(ctx, ref)
        answer = " ".join(line.strip() for line in bid_session.view.splitlines()
                          if line.strip() and line not in before and "[e" not in line)[:400]
        found = await mailbox.tan(answer)
        if not found:
            return f"{view}\n\nNo TAN found in the webmail. Request a new TAN with this tool again."
        bid_session.seen += "\n" + found.tan
        return f"{view}\n\nTAN from the webmail: {found.tan} (message {found.subject!r}, received {found.received})."

    tools = ((bidder_contact_email,) if not email else ()) + desk.tools
    batch = os.environ.get("AGENT_FILL_IN", "1") != "0"
    if batch:
        tools += (fill_in,)
    several = ("Enter everything a page asks for with one fill_in() call (all its fields, boxes and per-row "
               "buttons), then move on.\n" if batch else "")
    form_goal = f"""Fill in the bid for tender {briefing.tender_reference} on this website, following the brief's
submission steps below, up to the final review and signing step. Every value of this bid is already researched
and calculated; you only enter it: do not open documents, questions or other pages to check or research anything.
For this bid:
{_requirements(briefing, email, answer, desk)}
{several}Stop at the final review with the signing step: do not request a TAN and do not submit there.
Return as soon as that page shows. Refs change with every view: read a button's label in the latest view before
clicking it.

Brief's submission steps:
{briefing.submission_steps}"""
    left = deadline - time.monotonic()
    # the form never eats the signing reserve; with little time left, half of it still goes to the form
    form_deadline = deadline - SIGNING_RESERVE if left > SIGNING_RESERVE + 90 else time.monotonic() + left / 2
    ready, _ = await run_worker(bid_session, form_goal, FormReady, deadline=form_deadline,
                                max_steps=45 + 3 * len(desk.entries), notes=False,
                                thinking=llm.thinking("form", False), tools=tools)

    requirements = _requirements(briefing, email, answer, desk, filled=True)
    check = await check_summary(bid_session.view, requirements) if ready and ready.at_signing_step else None
    bid_session.trace.event(bid_session.name, "summary_check", **(check.model_dump() if check else {}))
    if check and check.matches:
        task = ("The form was filled in up to the signing step by an earlier pass, and its summary was checked "
                "against the requirements below; the current view shows it.")
    elif check:
        task = (f"The form was filled in up to the signing step by an earlier pass. A quick check of the summary "
                f"(which can be wrong) reported: {check.problems} Fix it only if the fix is on the current page; "
                f"never go back to earlier steps (that clears them). If it cannot be fixed here, sign anyway: a "
                f"signed bid with a flaw is worth far more than none.")
    else:
        task = ("An earlier pass started filling in the form but may not have finished; continue from the current "
                "view, and check the summary against the requirements below." +
                (f" It reported: {ready.open_points}" if ready and ready.open_points else ""))
    if os.environ.get("AGENT_STOP_BEFORE_SIGN") == "1":  # local timing runs: never request a TAN (shared webmail)
        address.cancel()
        return None
    review_goal = f"""Sign and submit the bid for tender {briefing.tender_reference} on this website. {task}
The bid must be:
{requirements}
Request the signing TAN only with request_signing_tan(ref of the website's TAN request button), never by clicking
that button yourself; enter the TAN it returns at once and submit bindingly. Browser confirmation dialogs are
accepted for you. Return once the website confirms the submission (or shows why it failed).

Brief's submission steps:
{briefing.submission_steps}"""
    result = None
    try:
        result, _ = await run_worker(bid_session, review_goal, SubmissionResult, deadline=deadline, max_steps=30,
                                     notes=False, thinking=llm.thinking("sign", False),
                                     tools=(*tools, request_signing_tan))
    except Exception as error:  # in a run, the platform closes the model token once the bid is in
        bid_session.trace.event(bid_session.name, "stopped", reason=f"{type(error).__name__}: {str(error)[:200]}")
    if not (result and result.submitted) and deadline - time.monotonic() > LAST_CHANCE:
        # the signing pass ran out of steps or got stuck: one short pass that only signs what is on screen
        last_goal = f"""Submit the bid for tender {briefing.tender_reference} now, as it stands. Do not change or
re-check any entry and never go back to earlier steps. If the signing step is not on screen, go forward to it.
Request the signing TAN with request_signing_tan(ref of the TAN request button), enter the TAN it returns at once
and submit bindingly. Return once the website confirms the submission (or shows why it failed)."""
        bid_session.trace.event(bid_session.name, "last_chance", seconds_left=round(deadline - time.monotonic()))
        try:
            result, _ = await run_worker(bid_session, last_goal, SubmissionResult, deadline=deadline, max_steps=15,
                                         notes=False, thinking=False, tools=(request_signing_tan,))
        except Exception as error:
            bid_session.trace.event(bid_session.name, "stopped",
                                    reason=f"{type(error).__name__}: {str(error)[:200]}")
    if not address.done():
        address.cancel()
    return result
