"""
Read the run's brief once: which tender, who bids, and which website plays which part.

Website ids and roles differ between datasets, so the model maps them from the brief and the run's access list
instead of the code knowing any site by name. The answer is checked against the brief.
"""
import llm
from browser_tools import unseen_values
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry


class Briefing(BaseModel):
    tender_reference: str = Field(description="The exact reference of the tender to bid on, as the brief writes it")
    company_name: str = Field(description="The bidder company name to register with, exactly as the brief gives it")
    vat_id: str = Field(description="The bidder's VAT id (USt-IdNr.) as the brief gives it, or '' if none")
    scenario_date: str = Field(description="The scenario date and time as the brief states it")
    bid_site: str = Field(description="siteId of the website where the bid is entered and signed")
    mail_site: str = Field(description="siteId of the bidder's own webmail, where approvals and the signing TAN arrive")
    submission_steps: str = Field(description="The brief's instructions for entering, signing and submitting the "
                                              "bid, copied word for word")


async def read_brief(task: dict) -> Briefing:
    sites = "\n".join(f"- siteId {s['siteId']}: {s['url']}" for s in task["access"])
    submission = task.get("submission", {})
    agent = Agent(llm.model(), output_type=Briefing, model_settings=llm.settings(thinking=True), retries=3,
                  instructions="You read a task brief and extract facts from it exactly as written.")

    @agent.output_validator
    def _from_brief(briefing: Briefing) -> Briefing:
        ids = {s["siteId"] for s in task["access"]}
        if briefing.bid_site not in ids or briefing.mail_site not in ids:
            raise ModelRetry(f"bid_site and mail_site must be among these siteIds: {sorted(ids)}")
        missing = unseen_values(briefing.model_dump_json(exclude={"bid_site", "mail_site"}), task["brief"])
        if missing:
            raise ModelRetry(f"{', '.join(missing)} are not in the brief; copy values exactly from it.")
        return briefing

    prompt = (f"Websites of this run:\n{sites}\n\nThe answer goes to: {submission}\n\nBrief:\n{task['brief']}")
    result = await agent.run(prompt)
    return result.output
