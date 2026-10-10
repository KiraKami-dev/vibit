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
from typing import Literal

import browser_tools
import llm
from briefing import Briefing
from browser_tools import Deps, SiteSession
from extract import locate
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from worker import run_worker

ADDRESS_WAIT = 15  # seconds the bid worker waits for the contact address before it starts without it
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
        async with self.lock:
            found, _ = await run_worker(
                self.session, "Find the email address of the mailbox you are signed in to (the bidder's own "
                "address, usually shown as the signed-in account). Return it at once.",
                MailboxAddress, deadline=self.deadline, max_steps=8, notes=False)
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
        async with self.lock:
            async with self.session.lock:  # the list the tab shows predates the request: load it again
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
    value: str = Field("", description="The text to type (fill) or the option's visible text (select)")


async def _perform(element, step: FormAction) -> None:
    if step.action == "fill":
        await element.fill(step.value, timeout=2000)
    elif step.action == "select":
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
                stop = f"{step.action} {step.ref} failed: {str(error).splitlines()[0][:200]}"
                break
            done.append(f"{step.action} {step.ref}")
            await asyncio.sleep(0.15)
            if page.is_closed() or page.url != url or len(session.pages) != windows:
                if step is not actions[-1]:
                    stop = "the page changed after it; the remaining actions were not done"
                break
        await asyncio.sleep(0.25)
        await session.settle()
        session.trace.event(session.name, "action", action="fill_in", done=done, stop=stop)
        view = await session.look()
    report = f"Done: {', '.join(done) or 'nothing'}." + (f" Stopped: {stop}." if stop else "")
    return f"{report}\n\n{view}"


def _requirements(briefing: Briefing, email: str) -> str:
    contact = repr(email) if email else "the contact email from bidder_contact_email()"
    return f"""- decision: bid (main offer, not an alternative offer);
- leave every position out with the form's option to omit a position (where the form shows one position per
  step, omit each in turn); do not configure materials, prices or lead times, and never use options that add
  positions;
- discount 0 and net total 0,00 (German number format);
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


async def submit_bid(bid_session: SiteSession, mailbox: Mailbox, briefing: Briefing, deadline: float
                     ) -> SubmissionResult | None:
    """v1: a bid with every position omitted, discount 0 % and net total 0, signed and submitted."""
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

    tools = (bidder_contact_email,) if not email else ()
    batch = os.environ.get("AGENT_FILL_IN", "1") != "0"
    if batch:
        tools += (fill_in,)
    several = ("Enter everything a page asks for with one fill_in() call (all its fields, boxes and per-row "
               "buttons), then move on.\n" if batch else "")
    form_goal = f"""Fill in the bid for tender {briefing.tender_reference} on this website, following the brief's
submission steps below, up to the final review and signing step. For this bid:
{_requirements(briefing, email)}
{several}Stop at the final review with the signing step: do not request a TAN and do not submit there.
Return as soon as that page shows. Refs change with every view: read a button's label in the latest view before
clicking it.

Brief's submission steps:
{briefing.submission_steps}"""
    ready, _ = await run_worker(bid_session, form_goal, FormReady, deadline=deadline, max_steps=45, notes=False,
                                thinking=llm.thinking("form", False), tools=tools)

    requirements = _requirements(briefing, email)
    check = await check_summary(bid_session.view, requirements) if ready and ready.at_signing_step else None
    bid_session.trace.event(bid_session.name, "summary_check", **(check.model_dump() if check else {}))
    if check and check.matches:
        task = ("The form was filled in up to the signing step by an earlier pass, and its summary was checked "
                "against the requirements below; the current view shows it.")
    elif check:
        task = (f"The form was filled in up to the signing step by an earlier pass, but a check of the summary "
                f"found: {check.problems} Fix that first (mind the page's warnings about going back).")
    else:
        task = ("An earlier pass started filling in the form but may not have finished; continue from the current "
                "view, and check the summary against the requirements below." +
                (f" It reported: {ready.open_points}" if ready and ready.open_points else ""))
    if os.environ.get("AGENT_STOP_BEFORE_SIGN") == "1":  # local timing runs: never request a TAN (shared webmail)
        address.cancel()
        return None
    careful = not (check and check.matches)
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
                                     notes=False, thinking=llm.thinking("sign", careful),
                                     tools=(*tools, request_signing_tan))
    except Exception as error:  # in a run, the platform closes the model token once the bid is in
        bid_session.trace.event(bid_session.name, "stopped", reason=f"{type(error).__name__}: {str(error)[:200]}")
    if not address.done():
        address.cancel()
    return result
