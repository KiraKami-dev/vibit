"""
Enter, sign and submit the bid: a worker on the bid website follows the brief's submission steps, and asks the
bidder's webmail (a second worker in its own tab) for the contact address and, once requested, the signing TAN.

The bid worker runs in two phases: filling the form up to the signing step (plain navigation, thinking off by
default), then requesting the TAN, entering it and submitting. A review page that was reached is signed as it
stands; one model call checks its summary against the requirements alongside, for the run logs only.
BidFlow starts the form's first steps while the research still runs.
THINKING_FORM, THINKING_REVIEW (the check) and THINKING_SIGN override these defaults (see llm.thinking).
"""
import asyncio
import os
import re
import time
from decimal import Decimal
from typing import Literal

import browser_tools
import llm
from briefing import Briefing
from browser_tools import Deps, SiteSession
from calc import position_key
from configurator import (NUMBER_ONLY, VALUE_NAMES, FillPlan, clean, complete_positions, decimal_mark, fill_position,
                          find_control, form_value, german, mapping_problems, parse_number, resolve_position, scoped,
                          unmapped)
from extract import locate
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from worker import run_worker, seconds_per_step

ADDRESS_WAIT = 15  # seconds the bid worker waits for the contact address before it starts without it
SIGNING_RESERVE = 180  # seconds kept for the review, the TAN round trip and submitting (seen: 25-30 s, 173 s)
LAST_CHANCE = 45  # seconds a final sign-only pass needs at least
HAND_OVER_AFTER = 2  # form failures of one position after which the model enters it itself with fill_in()
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
        The signing TAN just requested. The sites run on the scenario's clock, so "newest" is the request's own
        number or reference, else the highest request number or what the mailbox lists as newest, never a
        wall-clock time. `answer` is what the bid website said to the request (it may name the request), so the
        mail worker can pick that very message. A first try with thinking off, a second with thinking on.
        """
        older = f" The TAN {self.last_tan} you returned before is no longer valid." if self.last_tan else ""
        said = (f" The bid website answered the request with: {answer!r}; pick the message for that request."
                if answer else "")
        for thinking in (llm.thinking("tan", False), True):
            try:
                found = await self._tan(tan_goal(self.briefing.tender_reference, said, older), answer, thinking)
            except Exception as error:  # the bid worker asks again; an error here must not stop the bid
                self.session.trace.event(self.session.name, "stopped",
                                         reason=f"tan: {type(error).__name__}: {error}")
                found = None
            if found or self.deadline - time.monotonic() < TAN_RETRY_SECONDS:
                return found
        return None

    async def _tan(self, goal: str, answer: str, thinking: bool) -> SigningTan | None:
        async with self.lock:
            async with self.session.lock:  # the list the tab shows predates the request: load it again
                await self.session.revive()
                await self.session._goto(self.session.page.url)
            found, _ = await run_worker(self.session, goal, SigningTan, context=answer, deadline=self.deadline,
                                        max_steps=TAN_STEPS, notes=False, thinking=thinking)
        if found:
            self.last_tan = found.tan
        return found


TAN_STEPS = 16
TAN_RETRY_SECONDS = 60  # a second TAN lookup only with at least this much time left


def tan_goal(reference: str, said: str = "", older: str = "") -> str:
    """The TAN lookup's goal: what to find, not where a mailbox shows it."""
    return (f"A new signing TAN for tender {reference} was just requested.{said} Find the message with that TAN: "
            "a signing TAN for exactly this tender reference, and when the bid website or the messages show a "
            "request number or reference, the one for this request. Among several candidates take the newest "
            "request (its number or the mailbox's own order tells; never compare dates with today's clock). Use "
            "the mailbox's search or folders if it is not in view, and reload once if it has not arrived yet. If a "
            "preview stays empty, open the message's own page. Copy the TAN exactly from its text and return it "
            f"at once.{older}")


class FormAction(BaseModel):
    ref: str = Field(description="The element's ref in the latest view")
    action: Literal["fill", "select", "check", "uncheck", "click"]
    value: str = Field("", description="fill: the text to type; select: the option's visible text, exactly as the "
                                       "view lists it; empty for check, uncheck and click")


async def _perform(element, step: FormAction) -> None:
    if step.action == "fill":
        await element.fill(await number_field_text(element, step.value), timeout=2000)
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


async def number_field_text(element, text: str) -> str:
    """`text` for a field; a number-only field (type=number) takes "1234,50" only as "1234.50"."""
    try:
        number_only = await element.evaluate("e => e.tagName === 'INPUT' && (e.type || '').toLowerCase() === 'number'")
    except Exception:
        return text
    number = parse_number(text) if number_only else None
    return format(number, "f") if number is not None else text


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
        self.synonyms: dict[tuple[str, str], str] = {}  # (control label, wanted value) -> the option the model named
        self.next_button = ""
        self.asked_unmapped = False
        self.form_failures: dict[str, int] = {}  # failures of the form (not of a value) per position
        self.reserve = SIGNING_RESERVE  # seconds kept for signing (submit_bid sets it from the model pace)
        self.handed: set[str] = set()  # positions handed to the model to enter itself with fill_in()

    @property
    def mark(self) -> str:
        return self.plan.decimal

    def fmt(self, value, places: int = 2) -> str:
        """A figure as the form takes it (the brief's decimal mark, no thousands separators)."""
        return german(value, places, self.mark)

    @property
    def number_format(self) -> str:
        return f"decimal {'comma' if self.mark == ',' else 'point'}, no thousands separators"

    def by_hand(self, position: str) -> str:
        """The values of a position for the model to enter itself, as the form takes them."""
        entry = self.entries[position]
        values = "; ".join(f"{key}: {form_value(key, value, self.mark)}" for key, value in entry.items()
                           if key != "position")
        return values

    @property
    def tools(self) -> tuple:
        return (self.configure_position, self.net_total) if self.entries else ()

    @property
    def known(self) -> set[str]:
        """The value names a control can be mapped to."""
        return set(VALUE_NAMES).union(*self.entries.values()) - {"position"}

    def remember(self, fields: dict[str, str] | None, confirm_button: str = "", next_button: str = "") -> str:
        """
        Add to the control mapping and buttons. Unknown value names are dropped, the rest still counts; an error
        message only when that leaves no mapping at all, else ''.
        """
        if confirm_button:
            self.plan.confirm = confirm_button
        if next_button:
            self.next_button = next_button
        if fields:  # later calls add to or correct the mapping, they never drop what was mapped before
            known = self.known
            given = {label: name.strip() for label, name in fields.items()}
            unknown = sorted({name for name in given.values() if name not in known})
            given = {label: name for label, name in given.items() if name in known}
            self.plan.fields = {label: name for label, name in self.plan.fields.items() if name not in given.values()}
            self.plan.fields.update(given)
            if unknown and not self.plan.fields:
                return f"Unknown value names {unknown}; use only: {', '.join(sorted(known))}. Call again."
        return ""

    async def map_position_form(self, ctx: RunContext[Deps], fields: dict[str, str], confirm_button: str = "",
                                next_button: str = "") -> str:
        """
        Tell, on a position page and without entering anything, which control takes which value, so the positions
        can be entered at once when the values are ready. `fields`: control label (exactly as in your view) ->
        value name. `confirm_button`: label of the button that applies a choice so the next choice opens.
        `next_button`: label of the button that saves the position and goes on (never one that omits or adds).
        """
        problems = mapping_problems(ctx.deps.session.view, fields or {}, self.known, (confirm_button, next_button))
        if problems:
            return "Not taken: " + "; ".join(problems) + ". Fix the mapping and call again."
        self.remember(fields, confirm_button, next_button)
        ctx.deps.session.trace.event(ctx.deps.session.name, "position_form_mapped", fields=self.plan.fields,
                                     confirm=self.plan.confirm, next=self.next_button)
        return "Mapping taken. Enter nothing on this page; return now."

    async def configure_position(self, ctx: RunContext[Deps], position: str, fields: dict[str, str] | None = None,
                                 confirm_button: str = "", next_button: str = "",
                                 choices: dict[str, str] | None = None, entered_yourself: bool = False) -> str:
        """
        Enter all values of the position the form shows now and, once all are in, save it and go on with the form's
        next/continue button. `position`: its number as the page shows it. `fields`: control label (exactly as in
        your view) -> value name; needed on the first call, later calls reuse it. `confirm_button`: label of the
        button that applies a choice so the next choice opens. `next_button`: label of the button that saves the
        position and goes on (never the one that omits or adds a position); needed once. `choices`: only when a
        call reported it: control label -> the option to choose. `entered_yourself`: True only after you entered
        a position yourself because this tool handed it to you.
        """
        session, plan, asked = ctx.deps.session, self.plan, position.strip()

        def refuse(reason: str, text: str) -> str:  # every early return is traced: the run logs show why
            session.trace.event(session.name, "configure_refused", position=asked, reason=reason,
                                fields=fields or {}, text=text[:300])
            return text

        if entered_yourself:
            done = next((p for p in self.handed if p == asked or re.search(
                r"(?<![\w.])" + re.escape(p) + r"(?![\w]|\.\d)", asked)), None)
            if done is None:
                return refuse("not_handed", "Only a position this tool handed to you counts as entered by you; "
                                            "call configure_position() for the others.")
            self.entered.add(done)
            session.trace.event(session.name, "entered_by_model", position=done)
            return f"Position {done} counted as entered. {self.so_far(session)} Still to enter: {self.remaining()}."
        unknown = self.remember(fields, confirm_button, next_button)
        if unknown:
            return refuse("unknown_names", unknown)
        async with session.lock:
            view = await session.look()
            position, shown = resolve_position(asked, self.entries, view)
            if position is None or position not in shown:  # a page between two states: look again once settled
                await session.settle()
                view = await session.look()
                position, shown = resolve_position(asked, self.entries, view)
        if position is None:
            if shown:  # several of the bid's positions on one page: the model must name one of them
                return refuse("ambiguous", f"Name the position this page is about by its own number (one of "
                                           f"{', '.join(shown)}), then call again.")
            return refuse("no_values", f"No values for position {asked!r} (the page shows none of this bid's "
                                       f"positions {', '.join(sorted(self.entries, key=position_key))}): leave it "
                                       f"out with the form's option to omit a position. {self.so_far(session)}")
        if position not in shown:
            return refuse("not_on_page", f"Position {position} is not on the page now. Configure the position "
                                         "the page shows, by its own number; omit only a position that is not one "
                                         f"of {self.remaining()}.")
        entry = self.entries[position]
        if not plan.fields:
            return refuse("no_fields", f"Position {position} has values to enter. Give fields (control label -> "
                                       f"value name) for the controls of this form, then call again; do not omit it.")
        if position in self.handed:
            return refuse("handed", self.hand_over(position, session))
        if self.failures.get(position, 0) >= 3:
            return refuse("failed", f"Position {position} failed three times: leave it out with the form's option "
                                    f"to omit a position. {self.so_far(session)}")
        if time.monotonic() > self.deadline - self.reserve:
            return refuse("time", "Time is short: leave this and every remaining position out and go on to "
                                  "signing. " + self.so_far(session))
        values, labels = unmapped(view, entry, plan.fields)
        if values and labels and not self.asked_unmapped:  # asked once per run; the next call fills as mapped
            self.asked_unmapped = True
            return refuse("unmapped", f"Nothing entered yet. This bid has values for {', '.join(values)}, but no "
                                      f"control is mapped to them, while these fields of the page are not mapped: "
                                      f"{'; '.join(labels)}. Call again now with fields that add every one of them "
                                      "that takes such a value (the mapping you gave stays); leave out fields that "
                                      "match none.")
        async with session.lock:
            log: list[str] = []
            try:
                error = await fill_position(session, entry, plan, log, self.picks(position, entry, choices))
            except Exception as exc:  # a slow or changed page: the model decides to retry or omit
                error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
            moved = ""
            if not error and self.next_button:  # the model named the button; clicking it is the mechanical part
                view = await session.look()
                ref, why = find_control(view, self.next_button, ("button",))
                target = await locate(session.page, ref) if ref else None
                target, own = await scoped(session, target, position)
                others = [p for p in shown if p != position and p not in self.entered]
                if target is None:
                    moved = f" Could not save it: {why}; click the form's next/continue button yourself."
                elif others and not own:  # the page's button would leave the other positions it lists unentered
                    moved = (f" This page also shows {', '.join(others)}: enter each with configure_position(); "
                             f"{self.next_button!r} is clicked after the last of them.")
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
            if NUMBER_ONLY not in error:  # the form, not the value, stood in the way
                self.form_failures[position] = self.form_failures.get(position, 0) + 1
            if self.form_failures.get(position, 0) >= HAND_OVER_AFTER:
                self.handed.add(position)
                session.trace.event(session.name, "handed_to_model", position=position, error=error[:300])
                return f"Position {position} not entered: {error}. {self.hand_over(position, session)}\n\n{view}"
            return (f"Position {position} not fully entered: {error}. Entered so far: {'; '.join(log) or 'nothing'}. "
                    "Fix the mapping and call again, or leave the position out with the form's option to omit "
                    f"it. {self.so_far(session)}\n\n{view}")
        self.entered.add(position)
        then = moved or " Check it, then save and continue with the form's next/continue button."
        which = "" if position == asked else f" (the page showed {position}, not {asked!r}; {position} was entered)"
        return (f"Position {position} entered{which}: {'; '.join(log)}.{then} {self.so_far(session)} Still to "
                f"enter when the form shows them: {self.remaining()}.\n\n{view}")

    def hand_over(self, position: str, session: SiteSession) -> str:
        """The model enters a position itself after this tool failed on the form twice."""
        return (f"This tool failed on position {position} {HAND_OVER_AFTER} times, so enter it yourself now with "
                "fill_in(): make the choices in page order (with the button that applies a choice where one opens "
                "the next), then fill the fields, with exactly these values "
                f"({self.number_format}): {self.by_hand(position)}. Leave out controls that take none of them. "
                "Then save it with the form's next/continue button and call "
                f"configure_position({position!r}, entered_yourself=True). If it cannot be entered, leave it out "
                f"with the form's option to omit a position. {self.so_far(session)}")

    def remaining(self) -> str:
        """The bid's positions not entered yet: the form may show them in any order; none of them is omitted."""
        left = sorted(set(self.entries) - self.entered, key=position_key)
        return ", ".join(left) or "none"

    def picks(self, position: str, entry: dict, choices: dict[str, str] | None) -> dict[str, str]:
        """
        The model's option picks for this position's worded choices: what it named for this position (a revisit
        needs the same), and what it named for the same control and the same value on an earlier position (a
        "none" attribute that the form words differently comes up again and needs no second failed attempt).
        """
        remembered = self.choices.setdefault(position, {})
        remembered.update(choices or {})
        for label, option in (choices or {}).items():
            key = next((k for l, k in self.plan.fields.items() if clean(l) == clean(label)), None)
            if key is not None and entry.get(key) is not None:
                self.synonyms[(clean(label), clean(str(entry[key])))] = option
        picks = {label: self.synonyms[(clean(label), clean(str(entry[key])))]
                 for label, key in self.plan.fields.items()
                 if entry.get(key) is not None and (clean(label), clean(str(entry[key]))) in self.synonyms}
        picks.update(remembered)
        return picks

    def so_far(self, session: SiteSession) -> str:
        """The running net total, told after every position so the totals step needs no arithmetic."""
        total = self.fmt(self.total())
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
            extra = ", ".join(f"{label} {self.fmt(e[key])}" for key, label in (
                ("base_unit_price", "base price"), ("surcharge_per_unit", "surcharge per piece"),
                ("packaging_per_unit", "packaging per piece"), ("rebate_total", "bonus for the position"))
                if e.get(key) is not None)
            lines.append(f"  - position {p}: material {e['material_number']}, quantity {e['quantity']}, net unit "
                         f"price {self.fmt(e['unit_price'])}, lead time {e['lead_time_weeks']} weeks"
                         + (f", {extra}" if extra else ""))
        return "\n".join(lines)

    async def net_total(self, omitted: list[str] | None = None) -> str:
        """
        The bid's net total in the form's number format: quantity x unit price summed over the positions entered with
        configure_position(), minus any listed in `omitted` (positions you left out after all).
        """
        dropped = {p.strip() for p in omitted or []}
        return f"{self.fmt(self.total(dropped))} (positions {', '.join(sorted(self.entered - dropped)) or 'none'})"


def _positions(answer: dict | None, desk: PositionDesk, filled: bool, prepared: bool = False) -> str:
    """
    The decision and position part of the requirements: before the form phase, how to enter them; after it, what
    the review must show (the positions actually entered, all others omitted, the total of the entered ones).
    """
    discount = desk.fmt((answer or {}).get("discount_percent") or 0)
    if answer and answer.get("decision") == "no_bid":
        back = (" If the form shows a position step (an earlier pass chose the main offer before the decision was "
                "known), go back with the form's back button to the offer-type step and choose the declaration "
                "there: going back only clears steps that hold nothing yet;" if prepared and not filled else "")
        return ("- decision: no bid: choose the form's declaration that you do not submit an offer (not a main or "
                f"alternative offer); no positions;{back}\n- discount {discount} and net total {desk.fmt(0)};")
    head = "- decision: bid (main offer, not an alternative offer);\n"
    if filled and desk.entered:
        return head + (f"- exactly {len(desk.entered)} position(s) entered, each with these values:\n"
                       f"{desk.expected()}\n"
                       "- every other position omitted, and no added or extra position;\n"
                       f"- discount {discount} and net total {desk.fmt(desk.total())} ({desk.number_format}; never a "
                       "gross amount);")
    if not desk.entries:
        return head + ("- leave every position out with the form's option to omit a position (where the form shows "
                       "one position per step, omit each in turn); do not configure materials, prices or lead times, "
                       f"and never use options that add positions;\n- discount {discount} and net total "
                       f"{desk.fmt(0)} ({desk.number_format});")
    names = sorted({key for entry in desk.entries.values() for key in entry} - {"position"})
    if desk.plan.fields and desk.next_button:
        mapping = ("- configure_position() already knows which control takes which value and the buttons to apply a "
                   "choice and to save (an earlier pass mapped them on a position page): call it with the position "
                   "number alone; add fields only if it reports a control it cannot find;\n")
    else:
        mapping = f"- configure_position() needs, once, {mapping_guide(names)}\n"
    return head + f"""- enter exactly these positions: {', '.join(sorted(desk.entries))}. The form shows them in its own
  order (often not this list's order), one per step or several on one page. For every position the page shows,
  first call configure_position() with the number the page shows: it enters all values of a position of this bid
  and saves it with the next/continue button you name, or tells you to omit it. Never omit a position of this list
  unless configure_position() told you to omit that very position. Never pick options or type position values
  yourself, unless configure_position() hands a position to you with its values;
{mapping}- leave out, with the form's option to omit a position, every other position and any position where
  configure_position() reports a problem it cannot resolve. Never use options that add positions; if the form
  shows an extra position you did not ask for, remove it or leave it out;
- discount {discount}; net total: the latest figure configure_position() reports for the positions entered so
  far (net_total() repeats it; pass positions you entered but left out after all). Enter both exactly as given
  ({desk.number_format}), never a sum you work out yourself, a gross amount or a sum the page proposes;"""


def mapping_guide(names) -> str:
    """How to map a position page's controls to value names (configure_position and map_position_form)."""
    return f"""which control takes which value: map each control's label (as your view shows
  it) to one of these value names: {', '.join(names)}. Choices that open further choices (attributes of the
  material: catalogue group or product group, bore, stroke, ...) map to the attribute with the same meaning, in
  whatever form they come (lists, drop-downs, radio buttons); the material/catalogue number control to
  material_number; unit price, base price, surcharge, packaging and the bonus or rebate for the whole position to
  unit_price, base_unit_price, surcharge_per_unit, packaging_per_unit and rebate_total; lead time to
  lead_time_weeks. Leave out controls with no matching value (configure_position() asks for a choice it needs
  through `choices`). Also name the button that applies a choice so the next one opens, and the button that saves
  the position and goes on to the next step (not the ones that omit or add a position);"""


def _requirements(briefing: Briefing, email: str, answer: dict | None = None, desk: PositionDesk | None = None,
                  filled: bool = False, prepared: bool = False) -> str:
    contact = repr(email) if email else "the contact email from bidder_contact_email()"
    return f"""{_positions(answer, desk or PositionDesk({}, 0), filled, prepared)}
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
                     answer: dict | None = None, attributes: dict[str, dict[str, str]] | None = None,
                     flow: "BidFlow | None" = None) -> SubmissionResult | None:
    """
    Enter the calculated `answer` (calc's BidResult.answer()), sign and submit it. Positions the answer does not
    give completely, or that fail to enter, are omitted; without an answer every position is omitted (discount 0,
    total 0). `attributes` holds per position number the chosen material's attributes (e.g. its dimensions), which
    the form's choices lead through to the catalogue number. `flow` is the BidFlow that already did the steps
    before the positions (and the address lookup) while the research ran.
    """
    desk = flow.desk if flow else PositionDesk({}, deadline)
    desk.entries = complete_positions(answer or {}, attributes)
    desk.plan.decimal = decimal_mark(briefing.submission_steps)
    rewound = bool(flow and flow.rewound)
    prepared = bool(flow and flow.ready) and not rewound
    address = flow.contact_address() if flow else asyncio.create_task(mailbox.address())
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
{EARLIER_PASS if prepared else REWOUND if rewound else ""}For this bid:
{_requirements(briefing, email, answer, desk, prepared=prepared)}
{several}Stop at the final review with the signing step: do not request a TAN and do not submit there.
Return as soon as that page shows. Refs change with every view: read a button's label in the latest view before
clicking it.

Brief's submission steps:
{briefing.submission_steps}"""
    left = deadline - time.monotonic()
    # the form never eats the signing reserve, which grows with slow model calls; with little time left, half of
    # it still goes to the form
    desk.reserve = reserve = signing_reserve(left)
    form_deadline = deadline - reserve if left > reserve + 90 else time.monotonic() + left / 2
    bid_session.trace.event(bid_session.name, "form_budget", seconds=round(form_deadline - time.monotonic()),
                            reserve=round(reserve), pace=round(seconds_per_step(), 1))
    # not grounded: the result is a status, and its notes quote our own figures ("5,00" refused four times, 30 s,
    # after which the model went back a step looking for it)
    ready, _ = await run_worker(bid_session, form_goal, FormReady, deadline=form_deadline, grounded=False,
                                max_steps=45 + 3 * len(desk.entries), notes=False,
                                thinking=llm.thinking("form", False), tools=tools)

    requirements = _requirements(briefing, email, answer, desk, filled=True)
    at_review = bool(ready and ready.at_signing_step)

    async def logged_check(view: str) -> None:  # a record for the run logs only: signing never waits for it
        check = await check_summary(view, requirements)
        bid_session.trace.event(bid_session.name, "summary_check", **(check.model_dump() if check else {}))

    checking = asyncio.create_task(logged_check(bid_session.view)) if at_review else None
    if checking is None:
        bid_session.trace.event(bid_session.name, "summary_check")
    if at_review:
        # The review is signed as it stands, whatever the check says: a false alarm sent signing back and forth
        # between steps until its step cap (run 0d2d, ~100 s), and going back clears the later steps anyway.
        task = ("The form was filled in up to the signing step by an earlier pass; the current view shows its "
                "summary. Sign it as it stands: do not change, re-enter or re-check anything and never go back to "
                "earlier steps (that clears them); if the signing step is not on screen, go forward to it.")
    else:
        task = ("An earlier pass started filling in the form but may not have finished; continue from the current "
                "view, and check the summary against the requirements below." +
                (f" It reported: {ready.open_points}" if ready and ready.open_points else ""))
    if os.environ.get("AGENT_STOP_BEFORE_SIGN") == "1":  # local timing runs: never request a TAN (shared webmail)
        address.cancel()
        if checking is not None:
            await checking
        return None
    must = "" if at_review else f"The bid must be:\n{requirements}\n"
    review_goal = f"""Sign and submit the bid for tender {briefing.tender_reference} on this website. {task}
{must}Request the signing TAN only with request_signing_tan(ref of the website's TAN request button), never by clicking
that button yourself; enter the TAN it returns at once and submit bindingly. Browser confirmation dialogs are
accepted for you. Return once the website confirms the submission (or shows why it failed).

Brief's submission steps:
{briefing.submission_steps}"""
    contact = repr(email) if email else "the contact email from bidder_contact_email()"
    last_goal = f"""Submit the bid for tender {briefing.tender_reference} now. Keep every entry already made and never
go back to earlier steps. If the signing step is not on screen, go forward to it as fast as you can: on a step that
still asks for something, enter only what this list gives and leave every position not yet entered out with the
form's option to omit a position (never add one):
{_last_steps(briefing, contact, answer, desk)}
Request the signing TAN with request_signing_tan(ref of the TAN request button), enter the TAN it returns at once
and submit bindingly. Browser confirmation dialogs are accepted for you. Return once the website confirms the
submission (or shows why it failed)."""
    rescue = (request_signing_tan, fill_in) + ((bidder_contact_email,) if not email else ())
    passes = [(review_goal, 15 if at_review else 30,
               (request_signing_tan,) if at_review else (*tools, request_signing_tan))]
    result = None
    # A step cap never ends the signing while time is left: the short sign-only pass repeats until the website
    # confirms the bid or the time is up (run f5b1: a single last pass stopped at its cap with 176 s left).
    while True:
        goal, steps, sign_tools = passes[-1]
        try:
            result, _ = await run_worker(bid_session, goal, SubmissionResult, deadline=deadline, max_steps=steps,
                                         notes=False, thinking=llm.thinking("sign", False), tools=sign_tools)
        except Exception as error:  # in a run, the platform closes the model token once the bid is in
            bid_session.trace.event(bid_session.name, "stopped",
                                    reason=f"{type(error).__name__}: {str(error)[:200]}")
            await asyncio.sleep(2)
        if (result and result.submitted) or deadline - time.monotonic() <= LAST_CHANCE:
            break
        bid_session.trace.event(bid_session.name, "last_chance", seconds_left=round(deadline - time.monotonic()),
                                passes=len(passes))
        passes.append((last_goal, LAST_STEPS, rescue))
    for pending in (address, checking):
        if pending is not None and not pending.done():
            pending.cancel()
    return result


SIGN_STEPS = 10  # worker steps signing takes at worst: TAN request, the mailbox's lookup, entering it, confirming
LAST_STEPS = 15  # steps of one sign-only pass; passes repeat while time is left


def signing_reserve(left: float) -> float:
    """
    Seconds kept from the form for signing: SIGNING_RESERVE, or SIGN_STEPS at the pace model calls had lately
    (p95 30-90 s under load), but never more than 60 % of what is left, so the form still gets some time.
    """
    return min(max(SIGNING_RESERVE, SIGN_STEPS * seconds_per_step()), max(SIGNING_RESERVE, 0.6 * left))


def _last_steps(briefing: Briefing, contact: str, answer: dict | None, desk: PositionDesk) -> str:
    """The short list a sign-only pass needs to finish an incomplete form."""
    if answer and answer.get("decision") == "no_bid":
        decision = "the declaration that you do not submit an offer (no main or alternative offer), no positions"
        total = desk.fmt(0)
    else:
        decision = "the main offer (not an alternative offer)"
        total = desk.fmt(desk.total())
    discount = desk.fmt((answer or {}).get("discount_percent") or 0)
    return (f"- offer type: {decision};\n- bidder: company name {briefing.company_name!r}, VAT id "
            f"{briefing.vat_id!r}, contact email {contact};\n- discount {discount} and net total {total} "
            f"({desk.number_format});\n- tick the acknowledgement of the tender documents, not declarations that "
            "make your own terms and conditions apply.")


EARLIER_PASS = """An earlier pass already did the steps before the positions (bidder data, the main offer as offer type)
and stopped on the first position step, which the current view should show: continue from there. If the view
shows an earlier step instead (the portal session was renewed), do those steps again.
"""
REWOUND = """An earlier pass already entered the bidder data and went back to the offer-type step, which the current
view should show: choose the declaration not to bid there and continue. If the view shows an earlier step instead
(the portal session was renewed), do those steps again.
"""
REWIND_SECONDS = 90  # at most this long for a no-bid's way back to the offer-type step
REWIND_STEPS = 8


class OfferTypeStep(BaseModel):
    at_offer_type: bool = Field(description="True when the page now shows the step where the offer type is chosen "
                                            "(main offer, alternative offer, or the declaration not to bid)")
    step: str = Field(description="The step the page shows now, in a few words")


PREPARE_SECONDS = 180  # at most this long for the steps before the positions, while the research runs
KEEPALIVE_SECONDS = float(os.environ.get("AGENT_KEEPALIVE_SECONDS", "40"))  # unsaved form steps die with the session
PREPARES = 3  # passes at most: the first and two after a lost session
FINISH_WAIT = 45  # seconds the bid waits for a pass still on the form when the research ends
# the website's start page fetched from the tab (its cookies, no navigation): [final url, html]
KEEPALIVE_JS = ("async () => { const r = await fetch(location.origin + '/', {credentials: 'same-origin'}); "
                "return [r.url, await r.text()]; }")
SIGNED_OUT = re.compile(r"<input[^>]*type=[\"']?password", re.IGNORECASE)


class PrepareResult(BaseModel):
    at_positions: bool = Field(description="True when the page now shows the first step that asks for position "
                                           "values, the discount or the total")
    step: str = Field(description="The step the page shows now, in a few words")


class BidFlow:
    """
    The bid form overlapped with the research. Right after the briefing, a worker (the model) does the form's steps
    that need no calculated value (bidder data with the contact address, expression of interest, the main offer as
    offer type) and maps the controls of the first position page, then stops there. While the research runs, a
    cheap background request keeps the portal session (and with it the unsaved wizard state) alive; when the
    session is lost anyway, the page is reloaded and the steps are done again. finish() then enters the answer from
    the position step on, checks and signs (submit_bid). If the research ends in no_bid, a short pass goes back
    to the offer type, which clears only empty steps. AGENT_FORM_OVERLAP=0 turns this off.
    """

    def __init__(self, session: SiteSession, mailbox: Mailbox, briefing: Briefing, deadline: float):
        self.session, self.mailbox, self.briefing, self.deadline = session, mailbox, briefing, deadline
        self.desk = PositionDesk({}, deadline)
        self.address: asyncio.Task | None = None
        self.task: asyncio.Task | None = None
        self.ready = False  # a pass stopped at the positions and the session has not been lost since
        self.preparing = False
        self.passes = 0
        self.rewound = False  # a no-bid went back from the positions to the offer-type step
        self.desk.plan.decimal = decimal_mark(briefing.submission_steps)

    def start(self) -> None:
        self.address = asyncio.create_task(self.mailbox.address())
        if os.environ.get("AGENT_FORM_OVERLAP", "1") != "0":
            self.task = asyncio.create_task(self._run())

    def contact_address(self) -> asyncio.Task:
        """The address lookup; started again when the first one found nothing (e.g. the webmail was still down)."""
        if self.address is None or (self.address.done() and (self.address.cancelled() or not self.address.result())):
            self.address = asyncio.create_task(self.mailbox.address())
        return self.address

    def _event(self, kind: str, **data) -> None:
        self.session.trace.event("bid_flow", kind, **data)

    async def _run(self) -> None:
        while self.passes < PREPARES and self.deadline - time.monotonic() > SIGNING_RESERVE + 300:
            try:
                await self._prepare()
                if not self.ready:
                    return
                await self._keep_alive()  # returns only when the session was lost
                async with self.session.lock:  # sign in again and show where the wizard stands now
                    await self.session._goto(self.session.page.url)
            except Exception as error:  # e.g. a model error: another pass; at worst the form starts from scratch
                self.preparing = self.ready = False
                self._event("failed", error=f"{type(error).__name__}: {str(error)[:300]}")
                await asyncio.sleep(5)
        self.preparing = False

    async def _prepare(self) -> None:
        self.passes += 1
        self.preparing, self.ready = True, False
        started = time.monotonic()
        address = self.contact_address()

        async def bidder_contact_email(reason: str = "") -> str:
            """The bidder's contact email address, read from the bidder's own webmail."""
            found = await address
            self.session.seen += "\n" + found
            return found or "not found: the webmail did not show it"

        b = self.briefing
        goal = f"""Prepare the bid for tender {b.tender_reference} on this website while its values are still being
calculated. Follow the brief's submission steps below, but only those that need no calculated value: express
interest if the website asks for it, open the bid submission, register the bidder with company name
{b.company_name!r}, VAT id {b.vat_id!r} and the contact email from bidder_contact_email(), and choose the main offer
as the offer type (not an alternative offer, not a declaration that you do not bid). Save each of these steps
with the form's next/continue button. Do not open documents, questions or other pages.
Stop at the first step that asks for position values (materials, quantities, prices, lead times), the discount or
the total: enter, choose, omit, add or save nothing there. If that step shows one position, call
map_position_form() once with {mapping_guide(VALUE_NAMES)}
Then return. Refs change with every view: read a button's label in the latest view before clicking it.

Brief's submission steps:
{b.submission_steps}"""
        tools = (bidder_contact_email, self.desk.map_position_form, fill_in)
        until = min(started + PREPARE_SECONDS, self.deadline - SIGNING_RESERVE - 240)
        result, _ = await run_worker(self.session, goal, PrepareResult, deadline=until, max_steps=20, notes=False,
                                     grounded=False,
                                     thinking=llm.thinking("form", False), tools=tools)
        self.ready = bool(result and result.at_positions)
        self.preparing = False
        self._event("prepared", seconds=round(time.monotonic() - started, 1), at_positions=self.ready,
                    step=result.step if result else None, mapped=bool(self.desk.plan.fields), passes=self.passes)

    async def _keep_alive(self) -> None:
        """A light request to the website every KEEPALIVE_SECONDS; returns once the session is found signed out."""
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            try:  # from inside the page: the sandbox resolves the websites only in the browser, not in this process
                url, body = await asyncio.wait_for(self.session.page.evaluate(KEEPALIVE_JS), 20)
            except Exception as error:  # a slow website: try again next time
                self._event("keepalive_failed", error=f"{type(error).__name__}: {str(error)[:160]}")
                continue
            if SIGNED_OUT.search(body):
                self.ready = False
                self._event("session_lost", url=url)
                return

    async def rewind(self) -> bool:
        """
        A no-bid after the early pass chose the main offer: one short pass goes back to the offer-type step (the
        steps in between hold nothing yet), so the form pass starts where the declaration is chosen. When it does
        not get there, the form pass is told to go back itself (the `prepared` hint of _positions).
        """
        started = time.monotonic()
        goal = f"""The bid for tender {self.briefing.tender_reference} is a no-bid after all. An earlier pass chose the
main offer and went on to a later step of this form. Go back to the step where the offer type is chosen (main
offer, alternative offer or the declaration not to bid) with the form's own back/previous button or its step
navigation, one step at a time, without entering, choosing or saving anything on the way. Return as soon as the
offer-type step shows; do not choose anything there. Refs change with every view: read a button's label in the
latest view before clicking it."""
        until = min(started + REWIND_SECONDS, self.deadline - SIGNING_RESERVE - 60)
        try:
            result, _ = await run_worker(self.session, goal, OfferTypeStep, deadline=until, max_steps=REWIND_STEPS,
                                         notes=False, grounded=False, thinking=llm.thinking("form", False))
        except Exception as error:
            self._event("rewind_failed", error=f"{type(error).__name__}: {str(error)[:200]}")
            result = None
        self.rewound = bool(result and result.at_offer_type)
        self._event("rewound", ok=self.rewound, step=result.step if result else None,
                    seconds=round(time.monotonic() - started, 1))
        return self.rewound

    async def finish(self, answer: dict | None, attributes: dict[str, dict[str, str]] | None
                     ) -> SubmissionResult | None:
        """Enter the research's answer from where the form stands, check and sign."""
        if self.task is not None:
            waited = time.monotonic()
            while self.preparing and time.monotonic() - waited < FINISH_WAIT:  # a pass is still on the form
                await asyncio.sleep(0.5)
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self._event("handover", ready=self.ready, waited=round(time.monotonic() - waited, 1))
        if self.ready and answer and answer.get("decision") == "no_bid":
            await self.rewind()
        return await submit_bid(self.session, self.mailbox, self.briefing, self.deadline, answer, attributes,
                                flow=self)
