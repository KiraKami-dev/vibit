"""
Enter, sign and submit the bid: a worker on the bid website follows the brief's submission steps, and asks the
bidder's webmail (a second worker in its own tab) for the contact address and, once requested, the signing TAN.

The bid worker runs in two phases: filling the form up to the signing step (plain navigation, thinking off by
default), then checking the summary, requesting the TAN, entering it and submitting (thinking on by default).
THINKING_FORM and THINKING_REVIEW switch them (see llm.thinking).
"""
import asyncio

import browser_tools
import llm
from briefing import Briefing
from browser_tools import Deps, SiteSession
from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from worker import run_worker

ADDRESS_WAIT = 15  # seconds the bid worker waits for the contact address before it starts without it


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
                f"if it is not there yet), copy the TAN from its text and return it at once.{older}",
                SigningTan, context=answer, deadline=self.deadline, max_steps=8, notes=False)
        if found:
            self.last_tan = found.tan
        return found


def _requirements(briefing: Briefing, email: str) -> str:
    contact = repr(email) if email else "the contact email from bidder_contact_email()"
    return f"""- decision: bid (main offer, not an alternative offer);
- leave every position out with the form's option to omit a position; do not configure materials, prices or
  lead times, and never add extra positions;
- discount 0 and net total 0,00 (German number format);
- register with company name {briefing.company_name!r}, VAT id {briefing.vat_id!r} and contact email {contact};
- acknowledge the tender documents, but do not tick declarations that make your own terms and conditions apply."""


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
    form_goal = f"""Fill in the bid for tender {briefing.tender_reference} on this website, following the brief's
submission steps below, up to the final review and signing step. For this bid:
{_requirements(briefing, email)}
Stop at the final review with the signing step: do not request a TAN and do not submit there. Return as soon as
that page shows.

Brief's submission steps:
{briefing.submission_steps}"""
    ready, _ = await run_worker(bid_session, form_goal, FormReady, deadline=deadline, max_steps=45, notes=False,
                                thinking=llm.thinking("form", False), tools=tools)

    progress = ("The form was filled in up to the signing step by an earlier pass; the current view shows it."
                if ready and ready.at_signing_step else
                "An earlier pass started filling in the form but may not have finished; continue from the current "
                "view." + (f" It reported: {ready.open_points}" if ready and ready.open_points else ""))
    review_goal = f"""Sign and submit the bid for tender {briefing.tender_reference} on this website. {progress}
The bid must be:
{_requirements(briefing, email)}
Check the summary against this list and fix whatever differs (mind the page's warnings about going back). Then
request the signing TAN with request_signing_tan(ref of the website's TAN request button), enter the TAN it returns
at once, and submit bindingly. Browser confirmation dialogs are accepted for you. Return once the website confirms
the submission (or shows why it failed).

Brief's submission steps:
{briefing.submission_steps}"""
    result, _ = await run_worker(bid_session, review_goal, SubmissionResult, deadline=deadline, max_steps=30,
                                 notes=False, thinking=llm.thinking("review", True),
                                 tools=(*tools, request_signing_tan))
    if not address.done():
        address.cancel()
    return result
