"""
Measure what the site tools (search / collect / extract) save on read-only lookups: each task runs twice on the
scouting sites, with and without the tools, and the steps, seconds and tokens of both are compared.

Run from the repo root with the scouting logins and the models proxy (nothing is submitted; no bid form, no TAN):
    eval "$(atira scout --env)"; HEADLESS=1 uv run --project agent python dev/site_tools_check.py \
        --erp-query GROUP --mail-topic WORD [--spec] [--only erp mail spec] [--repeat N]

The query values are typed on the command line (a catalogue group from the specification, a topic word for the
mailbox) so that nothing from the sites lives in this script.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from playwright.async_api import async_playwright  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from pydantic_ai import Agent, ModelRetry  # noqa: E402

import browsers  # noqa: E402
import llm  # noqa: E402
import run as contract  # noqa: E402
from briefing import read_brief  # noqa: E402
from browser_tools import SiteSession  # noqa: E402
from site_tools import SITE_TOOLS  # noqa: E402
from tracing import Trace  # noqa: E402
from worker import run_worker  # noqa: E402


class Roles(BaseModel):
    erp_site: str = Field(description="siteId of the bidder's internal system with materials and prices")
    mail_site: str = Field(description="siteId of the bidder's mailbox")
    spec_site: str = Field(description="siteId of the website holding the buyer's technical specification")


async def roles(task: dict) -> Roles:
    ids = {s["siteId"] for s in task["access"]}
    sites = "\n".join(f"- siteId {s['siteId']}: {s['url']}" for s in task["access"])
    agent = Agent(llm.model(), output_type=Roles, model_settings=llm.settings(thinking=False), retries=3,
                  instructions="You read a task brief and say which website plays which part, using its siteIds.")

    @agent.output_validator
    def _known(out: Roles) -> Roles:
        if {out.erp_site, out.mail_site, out.spec_site} - ids:
            raise ModelRetry(f"use only these siteIds: {sorted(ids)}")
        return out

    return (await agent.run(f"Websites:\n{sites}\n\nBrief:\n{task['brief'].split('## Site access')[0]}")).output


class Hit(BaseModel):
    number: str = Field(description="Material number as shown")
    link: str = Field(description="Link target as shown")
    attributes: str = Field(description="The attributes the list shows for it, as shown")


class Hits(BaseModel):
    search_term: str
    hits: list[Hit]
    remark: str = Field("", description="What the site said about the result set, if anything")


class Message(BaseModel):
    subject: str
    sender: str
    date: str = Field(description="As shown, with any order tag")


class Messages(BaseModel):
    matching: list[Message] = Field(description="Every message the search listed")
    newest_subject: str = Field(description="Subject of the newest of them")
    newest_body_start: str = Field(description="The first sentence of the newest message's text, as shown")


class Revision(BaseModel):
    revision: str
    status: str


class Revisions(BaseModel):
    document: str
    revisions: list[Revision]


ERP_GOAL = """\
List the materials the system's material search finds for exactly the term {query}: open the search, search for
that term, and return every hit with its number, link target and the attributes the hit list shows. Do not refine
a search that lists hits; if the site says the term has too many or no hits, return that remark instead."""

MAIL_GOAL = """\
Find the messages in the mailbox about "{topic}" (search the mailbox for that word; check the archive folder too
if there is one). Return every matching message with subject, sender and date as shown, then open the newest one
and return the first sentence of its text. Newest means the latest date shown."""

SPEC_GOAL = """\
Find the buyer's technical specification document for the tender in the context (documents may be listed over
several pages). If the site asks for a confirmation before releasing documents, choose the binding documents and
copy any number it asks you to transfer exactly. Stop at the document's revision history and return every revision
it lists with its status as shown (a status may only be visible in a tooltip)."""

WITH_TOOLS = """

Tools that save steps: search(field_ref, query) types a query into the search field you name, submits it and
returns every row of the result list at once (following next / load-more to the end); collect("page") or
collect(ref of a row) returns a whole list or table the same way; extract({name: description, ...}) reads several
facts off the current view at once with their quotes. Prefer them over clicking through pages one by one."""


class Recording(Trace):
    """A trace that also keeps the worker's done/stopped events, for the comparison table."""

    def __init__(self):
        super().__init__()
        self.results: dict[str, dict] = {}
        self.extra: dict[str, dict] = {}  # tokens of the extract tool's own model calls, counted against the worker

    def event(self, worker: str, kind: str, quiet: bool = False, **data) -> None:
        super().event(worker, kind, quiet, **data)
        if kind in ("done", "stopped"):
            self.results[worker] = {"kind": kind, **data}
        if kind == "extract":
            extra = self.extra.setdefault(worker, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            extra["calls"] += 1
            extra["input_tokens"] += data.get("input_tokens") or 0
            extra["output_tokens"] += data.get("output_tokens") or 0


async def measure(site: dict, context, trace: Recording, tag: str, goal: str, output_type, *, ctx: str,
                  with_tools: bool, deadline: float) -> dict:
    name = f"{tag}:{'tools' if with_tools else 'plain'}"
    session = SiteSession({**site, "siteId": name}, context, trace)
    await session.start()
    started = time.monotonic()
    output, notes = await run_worker(session, goal + (WITH_TOOLS if with_tools else ""), output_type, context=ctx,
                                     deadline=deadline, max_steps=30, tools=SITE_TOOLS if with_tools else (),
                                     notes=False)
    seconds = round(time.monotonic() - started, 1)
    result = trace.results.get(name, {})
    extra = trace.extra.get(name, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
    for page in session.pages:
        try:
            await page.close()
        except Exception:
            pass
    return {"task": tag, "variant": "with tools" if with_tools else "plain", "ok": output is not None,
            "requests": (result.get("requests") or 0) + extra["calls"] if result else None, "seconds": seconds,
            "input_tokens": (result.get("input_tokens") or 0) + extra["input_tokens"] if result else None,
            "output_tokens": (result.get("output_tokens") or 0) + extra["output_tokens"] if result else None,
            "output": json.loads(output.model_dump_json()) if output is not None else result.get("reason")}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--erp-query", help="search term for the ERP material search (e.g. a catalogue group)")
    parser.add_argument("--mail-topic", help="a word to search the mailbox for")
    parser.add_argument("--spec", action="store_true", help="also the specification revision list")
    parser.add_argument("--only", nargs="*", default=["plain", "tools"], help="variants to run")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--seconds", type=float, default=420)
    args = parser.parse_args()

    task, _ = contract.load_run()
    trace = Recording()
    briefing, which = await asyncio.gather(read_brief(task), roles(task))
    access = {s["siteId"]: s for s in task["access"]}
    context = task["brief"].split("## Rules")[0].strip()
    print(f"roles: {which.model_dump()}; tender {briefing.tender_reference}", flush=True)

    jobs = []
    if args.erp_query:
        jobs.append(("erp-search", access[which.erp_site], ERP_GOAL.format(query=args.erp_query), Hits))
    if args.mail_topic:
        jobs.append(("mail-search", access[which.mail_site], MAIL_GOAL.format(topic=args.mail_topic), Messages))
    if args.spec:
        jobs.append(("spec-revisions", access[which.spec_site], SPEC_GOAL, Revisions))
    if not jobs:
        raise SystemExit("nothing to measure: give --erp-query, --mail-topic or --spec")
    variants = [v == "tools" for v in args.only]
    runs = [(job, with_tools) for job in jobs for with_tools in variants for _ in range(args.repeat)]
    async with async_playwright() as playwright:
        ctxs = await browsers.contexts(playwright, len(runs))
        deadline = time.monotonic() + args.seconds
        rows = await asyncio.gather(*(
            measure(site, ctx, trace, tag, goal, out, ctx=f"Tender reference: {briefing.tender_reference}\n{context}",
                    with_tools=with_tools, deadline=deadline)
            for ((tag, site, goal, out), with_tools), ctx in zip(runs, ctxs)))
    print("\n| task | variant | result | requests | seconds | input tokens | output tokens |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['task']} | {r['variant']} | {'ok' if r['ok'] else 'none'} | {r['requests']} | {r['seconds']} | "
              f"{r['input_tokens']} | {r['output_tokens']} |")
    for r in rows:
        print(f"\n===== {r['task']} ({r['variant']})\n{json.dumps(r['output'], ensure_ascii=False, indent=1)[:3000]}")
    print(f"\ntrace: {trace.path}")


if __name__ == "__main__":
    asyncio.run(main())
