"""
Enter, sign and submit the bid: a worker on the bid website follows the brief's submission steps, and asks the
bidder's webmail (a second worker in its own tab) for the contact address and, once requested, the signing TAN.
"""
import asyncio

from briefing import Briefing
from browser_tools import SiteSession
from pydantic import BaseModel, Field
from worker import run_worker


class MailboxAddress(BaseModel):
    address: str = Field(description="The email address of the mailbox you are signed in to")


class SigningTan(BaseModel):
    tan: str = Field(description="The TAN exactly as the message shows it")
    subject: str
    received: str = Field(description="When the message arrived, as shown")


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

    async def tan(self) -> SigningTan | None:
        """The newest signing TAN. The sites run on the scenario's clock, so "newest" is the highest request number
        or the top of the list, never a wall-clock time."""
        older = f" The TAN {self.last_tan} you returned before is no longer valid." if self.last_tan else ""
        async with self.lock:
            found, _ = await run_worker(
                self.session, f"A new signing TAN for tender {self.briefing.tender_reference} was just requested. "
                "Open the newest signing TAN message for exactly this tender reference (the highest request number; "
                "new TAN messages are listed without a search, newest first; reload the list once if needed), "
                f"copy the TAN from its text and return it at once.{older}",
                SigningTan, deadline=self.deadline, max_steps=8, notes=False)
        if found:
            self.last_tan = found.tan
        return found


async def submit_bid(bid_session: SiteSession, mailbox: Mailbox, briefing: Briefing, deadline: float
                     ) -> SubmissionResult | None:
    """v1: a bid with every position omitted, discount 0 % and net total 0, signed and submitted."""
    address = asyncio.create_task(mailbox.address())

    async def bidder_contact_email(reason: str = "") -> str:
        """The bidder's contact email address, read from the bidder's own webmail."""
        found = await address
        bid_session.seen += "\n" + found  # it comes from another website; let notes and results use it
        return found or "not found: the webmail did not show it"

    async def get_signing_tan(reason: str = "") -> str:
        """Call right after requesting the signing TAN on this website: returns the newest TAN for this tender
        from the bidder's webmail. A TAN is valid for five minutes and any new request invalidates it."""
        found = await mailbox.tan()
        if not found:
            return "No TAN found in the webmail. Request a new TAN and call this again."
        bid_session.seen += "\n" + found.tan
        return f"TAN {found.tan} (message {found.subject!r}, received {found.received})"

    goal = f"""Enter, sign and submit the bid for tender {briefing.tender_reference} on this website, following the
brief's submission steps below. For this bid:
- decision: bid (main offer, not an alternative offer);
- leave every position out with the form's option to omit a position; do not configure materials, prices or
  lead times, and never add extra positions;
- discount 0 and net total 0,00 (German number format);
- register with company name {briefing.company_name!r}, VAT id {briefing.vat_id!r} and the contact email from
  bidder_contact_email();
- acknowledge the tender documents, but do not tick declarations that make your own terms and conditions apply;
- request the signing TAN, call get_signing_tan() right away, enter that TAN, check the summary and submit
  bindingly. Browser confirmation dialogs are accepted for you.
Return once the website confirms the submission (or shows why it failed).

Brief's submission steps:
{briefing.submission_steps}"""
    result, _ = await run_worker(bid_session, goal, SubmissionResult, deadline=deadline, max_steps=70,
                                     thinking=True, tools=(bidder_contact_email, get_signing_tan))
    if not address.done():
        address.cancel()
    return result
