"""
Research workers for the buyer's specification and the bidder's ERP (roadmap v2/v3).

Which website plays which part comes from the brief through a model, never from a siteId or URL. On each website a
model navigates and decides; helpers only read what it chose:

- Specification: a worker finds the tender's specification document, passes its access confirmation, decides which
  revision is the current one and opens it; `read_document` then pages through that revision along the control
  the model pointed at, and a model extracts the requirements page by page (in parallel).
- ERP: per requirement a worker searches the catalogue group the specification names and lists the hits with
  their attributes; per hit a worker opens the views with status, stock, lead times, carton size, masses, index
  code and every price condition with its scales (`open_pages` opens several chosen views in one step), and a model
  reads them into a record. A model then chooses the material (thinking on); `evidence.check_choice` validates the
  exact match and active status and sends a wrong choice back. Requirements and materials run in parallel, each
  in its own tab.

Usable-travel reserves and scoped amendments come from other workers (mail, tender Q&A): `evidence.
effective_requirement` takes them, and `reselect` lets the model choose again once they are known, because the
ERP stage inspects every hit of the group.
"""
import asyncio
import inspect
import os
import re
import time
from decimal import Decimal
from typing import TypeVar
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

import calc
import llm
from briefing import Briefing
from browser_tools import Deps, SiteSession, unseen_values
from evidence import (
    Candidate,
    EffectiveRequirement,
    MaterialRecord,
    ParseError,
    Requirement,
    RequirementResearch,
    SpecResult,
    SpecRevision,
    check_choice,
    merged_attributes,
    number_format,
    parse_date,
    parse_int,
    parse_mass_kg,
    parse_number,
    parse_weeks,
)
from tracing import Trace
from worker import run_worker

T = TypeVar("T", bound=BaseModel)

_REF = re.compile(r"\[e\d+ ")


def on_site(site: dict, url: str) -> str | None:
    """The absolute URL of a link target if it is on the site's own host (the goto tool's rule), else None."""
    target = urljoin(site["url"], url)
    return target if urlparse(target).netloc == urlparse(site["url"]).netloc else None


def _strip_refs(text: str) -> str:
    return _REF.sub("[", text)


def _read_settings(thinking: bool):
    """Model settings for a call reading a whole page (often 15 to 40 s): the timeout is LLM_READ_TIMEOUT (default
    90 s) in either mode, since the thinking-off default is tuned for short navigation steps."""
    return llm.settings(thinking, timeout=llm.env_seconds("LLM_READ_TIMEOUT", 90.0))


# Readers copy values from pages a worker chose, under a grounding check; thinking makes each call ~30 s instead of
# ~5 s for little gain there. Judgement calls (which revision governs) always think.
READ_THINKING = os.environ.get("RESEARCH_READ_THINKING", "0") == "1"


async def _grounded_read(output_type: type[T], instructions: str, prompt: str, source: str, *,
                         trace: Trace | None = None, name: str = "research", what: str = "") -> T:
    """One reader call: the model reports what `source` (the text it is shown) says; every value it returns must
    appear in that text, else it is told which ones do not and tries again. Traced with seconds and refusals."""
    agent = Agent(llm.model(), output_type=output_type, instructions=instructions,
                  model_settings=_read_settings(READ_THINKING), retries=3)
    refusals: list[str] = []

    @agent.output_validator
    def _grounded(out):
        missing = unseen_values(out.model_dump_json(), source)
        if missing:
            refusals.extend(missing[:10])
            raise ModelRetry(f"{', '.join(missing[:10])} are not in the text; copy values exactly from it.")
        return out

    started = time.monotonic()
    try:
        return (await agent.run(prompt)).output
    finally:
        if trace:
            trace.event(name, "read", what=what, seconds=round(time.monotonic() - started, 1), refusals=refusals)


def same_revision(a: str, b: str) -> bool:
    """Revision labels as reported may carry a word around the id ("Revision R-2" vs "R-2")."""
    a, b = " ".join(a.split()).casefold(), " ".join(b.split()).casefold()
    return bool(a and b) and (a == b or a in b or b in a)


def _worker_kwargs(**kwargs) -> dict:
    """Pass optional run_worker options only when this harness version has them."""
    params = inspect.signature(run_worker).parameters
    return {k: v for k, v in kwargs.items() if k in params}


# ---------------------------------------------------------------- which website is which


class ResearchSites(BaseModel):
    spec_site: str = Field(description="siteId of the website that holds the buyer's technical specification and "
                                       "its revisions")
    erp_site: str = Field(description="siteId of the bidder's own internal system with materials, stock, lead "
                                      "times and price conditions")


async def research_sites(task: dict) -> ResearchSites:
    """Map the specification and ERP roles to siteIds from the brief (the private run renames and re-splits)."""
    ids = {s["siteId"] for s in task["access"]}
    sites = "\n".join(f"- siteId {s['siteId']}: {s['url']}" for s in task["access"])
    agent = Agent(llm.model(), output_type=ResearchSites, model_settings=llm.settings(thinking=False), retries=3,
                  instructions="You read a task brief and say which website plays which part, using its siteIds.")

    @agent.output_validator
    def _known(out: ResearchSites) -> ResearchSites:
        if out.spec_site not in ids or out.erp_site not in ids:
            raise ModelRetry(f"spec_site and erp_site must be among these siteIds: {sorted(ids)}")
        return out

    brief = task["brief"].split("## Site access")[0]
    result = await agent.run(f"Websites of this run:\n{sites}\n\nBrief:\n{brief}")
    return result.output


# ---------------------------------------------------------------- tools that read what the model chose

_DOCUMENTS: dict[int, list[str]] = {}  # id(session) -> page texts read by read_document


def _common_edges(texts: list[str]) -> tuple[int, int]:
    """Lines shared at the start and at the end of every text (site chrome), to show them only once."""
    lines = [t.splitlines() for t in texts]
    if len(lines) < 2:
        return 0, 0
    head = 0
    while all(len(x) > head for x in lines) and len({x[head] for x in lines}) == 1:
        head += 1
    tail = 0
    while all(len(x) > head + tail for x in lines) and len({x[-1 - tail] for x in lines}) == 1:
        tail += 1
    return head, tail


async def open_pages(ctx: RunContext[Deps], paths: list[str]) -> str:
    """Read several pages of this website in one step, e.g. the tabs, sub-views or records of what you are
    investigating. Give link targets exactly as pages you have seen show them (at most 8). The texts come back
    without refs; you end up on the last one (read_more(offset=0) shows its refs)."""
    session = ctx.deps.session
    texts, names = [], []
    for path in paths[:8]:
        parsed = urlparse(path)
        rel = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if path not in session.seen and rel not in session.seen:
            texts.append("not opened: no page you have seen links there")
            names.append(path)
            continue
        target = urljoin(session.page.url, path)
        if urlparse(target).netloc != urlparse(session.site["url"]).netloc:
            texts.append("not opened: not on this website")
            names.append(path)
            continue
        async with session.lock:
            await session._goto(target)
            session.trace.event(session.name, "action", action="open_pages", url=target)
            view = await session.look()
        landed = urlparse(session.page.url)
        wanted = urlparse(target)
        if (landed.path, landed.query) != (wanted.path, wanted.query):
            texts.append(f"did not open: the site showed {session.page.url} instead (handle that page, then open "
                         f"this view again)\n" + _strip_refs(view.split("\n\n", 1)[-1])[:3000])
            names.append(path)
            continue
        texts.append(_strip_refs(view.split("\n\n", 1)[-1])[:8000])
        names.append(path)
    def opened(text: str) -> bool:
        return not text.startswith(("not opened", "did not open"))

    head, tail = _common_edges([t for t in texts if opened(t)])
    out = []
    for i, (name, text) in enumerate(zip(names, texts)):
        if i and opened(text) and (head or tail):
            lines = text.splitlines()
            text = "\n".join(lines[head:len(lines) - tail]) + "\n[page frame as in the first page]"
        out.append(f"=== {name}\n{text}")
    return "\n\n".join(out)


def _control(view: str, ref: str) -> tuple[str, str] | None:
    m = re.search(r"\[" + re.escape(ref) + r' (\w+) "([^"]*)"', view)
    return (m.group(1), m.group(2)) if m else None


async def _click(element) -> None:
    try:
        await element.click(timeout=5000)
    except Exception:
        await element.evaluate("e => e.click()")


async def read_document(ctx: RunContext[Deps], next_ref: str = "") -> str:
    """Read the whole document you have open: give the ref of its control that shows the next page or loads more
    (next_ref), and every page is read in turn and kept for extraction. Leave next_ref empty when the document has
    a single page. Call it while the document version you chose is open."""
    session = ctx.deps.session
    pages = [session.view]
    control = _control(session.view, next_ref) if next_ref else None
    if next_ref and control is None:
        return f"No control {next_ref} in the current view; use a ref from the latest view."
    if control:
        role, label = control
        pattern = re.compile(r"\[(e\d+) " + re.escape(role) + r' "' + re.escape(label) + r'"(?![^\]]*disabled)')
        nth = [m.group(1) for m in pattern.finditer(session.view)].index(next_ref)
        seen = {_strip_refs(session.view)}
        for _ in range(40):
            refs = [m.group(1) for m in pattern.finditer(session.view)]
            if not refs:
                break
            await session.act("click", refs[min(nth, len(refs) - 1)], _click)
            text = _strip_refs(session.view)
            if text in seen:  # past the end some sites repeat the last page
                break
            seen.add(text)
            pages.append(session.view)
    # "load more" pages contain the earlier ones: keep only pages no later page contains
    stripped = [_strip_refs(p) for p in pages]
    keep = [p for i, p in enumerate(stripped) if not any(p in later for later in stripped[i + 1:])]
    _DOCUMENTS[id(session)] = keep
    session.trace.event(session.name, "read_document", pages=len(pages), kept=len(keep),
                        chars=sum(len(p) for p in keep))
    return (f"Read {len(pages)} views of the document ({len(keep)} distinct, {sum(len(p) for p in keep)} "
            f"characters); they are kept for extraction. Return your result now.\n\n" + await session.look())


# ---------------------------------------------------------------- specification


class RevisionSeen(BaseModel):
    revision: str = Field(description="Revision identifier as shown")
    status: str = Field(description="Its status as shown, including statuses only visible in tooltips")


class RevisionHistory(BaseModel):
    document: str = Field(description="The specification document's number or title as shown")
    revisions: list[RevisionSeen] = Field(description="Every revision the document's history lists")


class CurrentRevision(BaseModel):
    revision: str = Field(description="The revision the site marks as currently valid, exactly as shown")
    why: str = Field(description="What on the page marks it as current, in a few words")


class Opened(BaseModel):
    done: bool = Field(description="True once read_document has read the requested revision")


class RequirementSeen(BaseModel):
    requirement_id: str = Field(description="The requirement's reference as shown")
    bore: str = Field(description="Bore in mm, exactly as shown")
    stroke: str = Field(description="Stroke (travel) in mm, exactly as shown")
    pressure: str = Field(description="Operating pressure in bar, exactly as shown")
    mounting: str = Field(description="Mounting type as written")
    certification: str = Field(description="Required certification as written, or the words saying none is "
                                           "required")
    catalog_group: str = Field(description="The Kataloggruppe (catalogue group code) named for the ERP search")
    pieces_per_set: str = Field("", description="Pieces per set (Satz) if the text defines a set, else ''")
    stroke_is_usable_travel: bool = Field(description="True if the stroke is stated as usable travel (nutzbarer "
                                                      "Weg) to which a reserve must be added for the ERP stroke")
    reserve_key: str = Field("", description="The key of that end reserve, if named, else ''")
    conflict: str = Field("", description="Only if the table and the full wording disagree: how; else ''")


class SpecPage(BaseModel):
    revision_shown: str = Field(description="The revision this page belongs to, as shown on it, or ''")
    requirements: list[RequirementSeen]


FIND_SPEC_GOAL = """\
Find the buyer's technical specification for the tender in the context (match it by the tender's reference or
subject; documents may be listed over several pages) and open it. If the site asks for a confirmation before
releasing documents, choose the option that gives access to the binding documents (archive copies or previews
are not the governing ones) and copy any number it asks you to transfer exactly. Stop at the document's revision
history and return every revision it lists with its status as shown (statuses may only be visible in tooltips)."""

CHOOSE_REVISION = """\
You decide which revision of a buyer's specification governs a bid: the one the document marks as currently valid
(in whatever words the site uses), never one marked as superseded, replaced or withdrawn. Documents may list or open the
oldest revision first, and dates may be written in different orders, so go by the marked status; use dates only if
no status marks one revision as current."""

OPEN_REVISION_GOAL = """\
Open the requirements of revision {revision} of the specification document you have open (not another revision).
Once its first page of requirements shows, call read_document with the ref of the control that shows the next page
or loads more (leave it empty if everything is on one page), so that all its requirements are read; then return."""

EXTRACT_INSTRUCTIONS = """\
You extract requirements from one page of a technical specification exactly as written. Copy numbers as shown
including any locale tag next to them; never convert, compute or guess. Each requirement may appear both as
a table row and in its full original wording (which wins if they differ). Report only requirements that this page
shows; return an empty list if it shows none."""


async def _extract_page(text: str, document: str, revision: str, context: str, trace: Trace | None,
                        name: str) -> SpecPage | None:
    prompt = f"Document {document}, revision {revision} (the current one).\nContext: {context}\n\nPage:\n{text}"
    try:
        return await _grounded_read(SpecPage, EXTRACT_INSTRUCTIONS, prompt, text, trace=trace, name=name,
                                    what="spec page")
    except Exception as error:  # one bad page must not sink the rest
        print(f"[research] spec page extraction failed: {type(error).__name__}: {str(error)[:200]}", flush=True)
        return None


async def _choose_revision(history: RevisionHistory, view: str, context: str) -> CurrentRevision | None:
    ids = {r.revision.strip() for r in history.revisions}
    agent = Agent(llm.model(), output_type=CurrentRevision, instructions=CHOOSE_REVISION,
                  model_settings=_read_settings(True), retries=3)

    @agent.output_validator
    def _listed(out: CurrentRevision) -> CurrentRevision:
        if not any(same_revision(out.revision, i) for i in ids):
            raise ModelRetry(f"Choose one of the listed revisions: {sorted(ids)}")
        return out

    listing = "\n".join(f"- {r.revision}: {r.status}" for r in history.revisions)
    prompt = (f"{context}\n\nDocument: {history.document}\nRevisions as reported:\n{listing}\n\n"
              f"The revision history page:\n{_strip_refs(view)[:12000]}")
    try:
        return (await agent.run(prompt)).output
    except Exception as error:
        print(f"[research] revision choice failed: {type(error).__name__}: {str(error)[:200]}", flush=True)
        return None


def _parse_requirement(seen: RequirementSeen, revision: str) -> Requirement:
    return Requirement(
        requirement_id=seen.requirement_id.strip(), revision=revision, bore=parse_number(seen.bore),
        stroke=parse_number(seen.stroke), pressure=parse_number(seen.pressure), mounting=seen.mounting.strip(),
        certification=seen.certification.strip(), catalog_group=seen.catalog_group.strip(),
        pieces_per_set=parse_int(seen.pieces_per_set) if seen.pieces_per_set.strip() else None,
        stroke_is_usable=seen.stroke_is_usable_travel, reserve_key=seen.reserve_key.strip() or None,
        raw={k: str(v) for k, v in seen.model_dump().items()})


async def research_spec(session: SiteSession, briefing: Briefing, *, context: str = "",
                        deadline: float | None = None, on_requirement=None, on_revision=None,
                        thinking: bool = False) -> SpecResult:
    """
    The current specification revision's requirements: a worker finds the document and its revision history
    (navigation, thinking off), a model decides the current revision (thinking on), the worker opens it and pages
    through it, and a model extracts each page (thinking on, in parallel).

    `on_revision()` runs once the governing revision is chosen (e.g. to warm up the ERP), `on_requirement(req)` as
    soon as each requirement is parsed, so ERP research can start before the last page is read. `thinking`
    turns thinking on for finding the document (a second attempt after a fast one got lost).
    """
    started = time.monotonic()
    result = SpecResult()
    goal_context = f"Tender reference: {briefing.tender_reference}\n{context}".strip()

    def finish(problem: str | None = None) -> SpecResult:
        if problem:
            result.problems.append(problem)
        result.seconds = round(time.monotonic() - started, 1)
        session.trace.event(session.name, "spec_done", seconds=result.seconds, revision=result.current_revision,
                            requirements=[r.requirement_id for r in result.requirements], problems=result.problems)
        return result

    history, _ = await run_worker(session, FIND_SPEC_GOAL, RevisionHistory, context=goal_context, deadline=deadline,
                                  max_steps=25, **_worker_kwargs(notes=False, thinking=thinking))
    if history is None or not history.revisions:
        return finish("no revision history found")
    result.document = history.document
    result.revisions = [SpecRevision(revision=r.revision, status_shown=r.status) for r in history.revisions]
    choice = await _choose_revision(history, session.view, goal_context)
    if choice is None:
        return finish("no current revision chosen")
    result.current_revision = choice.revision.strip()
    session.trace.event(session.name, "spec_revision", revision=result.current_revision, why=choice.why)
    if on_revision:
        on_revision()

    for _ in range(2):
        await run_worker(session, OPEN_REVISION_GOAL.format(revision=result.current_revision), Opened,
                         context=goal_context, deadline=deadline, max_steps=12, tools=(read_document,),
                         **_worker_kwargs(notes=False, grounded=False))
        pages = _DOCUMENTS.pop(id(session), [])
        if pages:
            break
    else:
        return finish(f"revision {result.current_revision} could not be read")
    session.trace.event(session.name, "spec_extract", pages=len(pages))
    by_id: dict[str, Requirement] = {}

    async def extract(text: str) -> SpecPage | None:
        page = await _extract_page(text, history.document, result.current_revision, goal_context, session.trace,
                                   session.name)
        if page and on_requirement and not (page.revision_shown and not same_revision(page.revision_shown,
                                                                                      result.current_revision)):
            for seen in page.requirements:
                try:
                    req = _parse_requirement(seen, result.current_revision)
                except ParseError:
                    continue
                if req.requirement_id not in streamed:
                    streamed.add(req.requirement_id)
                    on_requirement(req)
        return page

    streamed: set[str] = set()
    extracted = await asyncio.gather(*(extract(p) for p in pages))
    for page in extracted:
        if page is None:
            result.problems.append("a document page could not be extracted")
            continue
        if page.revision_shown and not same_revision(page.revision_shown, result.current_revision):
            result.problems.append(f"a page shows revision {page.revision_shown}, not {result.current_revision}")
            continue
        for seen in page.requirements:
            try:
                req = _parse_requirement(seen, result.current_revision)
            except ParseError as error:
                result.problems.append(f"{seen.requirement_id}: {error}")
                continue
            if seen.conflict:
                result.problems.append(f"{req.requirement_id}: {seen.conflict}")
            if req.requirement_id in by_id and by_id[req.requirement_id].raw != req.raw:
                result.problems.append(f"{req.requirement_id} read differently on two pages")
            by_id.setdefault(req.requirement_id, req)
    result.requirements = sorted(by_id.values(), key=lambda r: calc.position_key(r.requirement_id))
    return finish()


# ---------------------------------------------------------------- ERP


class Ready(BaseModel):
    page: str = Field(description="What the page you ended on offers, in a few words")


class CandidateSeen(BaseModel):
    material_number: str
    link: str = Field(description="The hit's link target exactly as shown")
    bore: str = Field("", description="Bore in mm as shown, '' if not shown")
    stroke: str = Field("", description="Stroke in mm as shown, '' if not shown")
    pressure: str = Field("", description="Pressure in bar as shown, '' if not shown")
    mounting: str = Field("", description="Mounting as shown, '' if not shown")
    certification: str = Field("", description="Certification code as shown; '' if the hit names none")


class SearchReport(BaseModel):
    search_term: str = Field(description="The term you searched for, as typed")
    candidates: list[CandidateSeen] = Field(description="Every hit of the search")


class Coverage(BaseModel):
    not_found: list[str] = Field(description="Facts you could not find on any view you opened; [] if all found")


class ScaleSeen(BaseModel):
    min_quantity: str = Field(description="Scale quantity from which the price applies, as shown")
    price: str = Field(description="Price per price unit as shown, with any locale tag")


class ConditionSeen(BaseModel):
    valid_from: str
    valid_to: str
    currency: str
    fx_key: str = Field("", description="Exchange-rate key named with the currency, '' if none")
    price_unit: str = Field(description="Number of pieces the price refers to, as shown")
    number_format: str = Field("", description="Number format the record declares, '' if none")
    scales: list[ScaleSeen] = Field(description="Every quantity scale of this record; [] if never opened")


class MaterialSeen(BaseModel):
    material_number: str
    status_code: str = Field(description="The material status as shown")
    status_meaning: str = Field(description="What that status means per the page's legend or text")
    active: bool = Field(description="True if the status means released/usable, False if blocked or similar")
    bore: str = ""
    stroke: str = ""
    pressure: str = ""
    mounting: str = ""
    certification: str = Field("", description="As the technical data show it (also tooltips)")
    free_stock: str = Field(description="Freely usable (unrestricted) stock, NOT counting blocked, inspection or "
                                        "otherwise unavailable stock, as shown")
    blocked_stock: str = Field("", description="Blocked or otherwise unavailable stock as shown, '' if none")
    stock_lead_time: str = Field(description="Lead time for delivery from stock, as shown with unit")
    production_lead_time: str = Field(description="Production lead time, as shown with unit")
    pieces_per_carton: str = Field("", description="Pieces per carton/pack as shown")
    allocation_mass: str = Field("", description="Allocation weight per piece with unit (for cost allocation; "
                                                 "distinct from the surcharge mass)")
    surcharge_mass: str = Field("", description="Mass per piece for the metal surcharge, with unit")
    metal_index: str = Field("", description="Metal/price index code as shown")
    conditions: list[ConditionSeen] = Field(description="Every price condition record shown, newest and older")


SEARCH_GOAL = """\
List the ERP materials of catalogue group {group}: use the ERP's material search (search help) with exactly that
group as the search term. As soon as the hit list shows, return every hit of that group with its link target and
the technical attributes (bore, stroke, pressure, mounting, certification) the list shows; do not refine a search
that already lists hits. Only if the list shows no attributes, open the hits to read them. The materials are
compared with the requirement later ({want})."""

DETAIL_GOAL = """\
Open the views of ERP material {material} (you start on its record) that show these facts: its status and the
meaning of that status (a legend may sit in a tooltip); its technical data; its stock, down to the view that
separates freely usable from blocked or inspection stock; lead time from stock and production lead time; pieces
per carton, allocation weight, surcharge mass and metal/price index code; and EVERY price condition record with
its validity, currency, exchange-rate key, price unit and quantity scales (records may be paged one per page, and
scales may sit in a separate view of each record; a record list that says it has one record has no others). Use
open_pages to open several views you have seen links to in one step, then follow the further links those views
show; never guess addresses. If a sign-in, notice or error page interrupts, get past it and open again what it
replaced. Every view you open is kept for a reader, so you do not need to copy values: return as soon as all of
these have been opened, listing only facts you could not find.{focus}"""

MATERIAL_INSTRUCTIONS = """\
You read the ERP views of one material and report its facts exactly as written. Copy numbers as shown with units
and any locale tag next to them; never convert, compute or guess. Status: the code shown for the
material and its meaning from the page's legend (tooltips count; hidden text does not appear here). Free stock is
the freely usable quantity only, never a total that includes blocked or inspection stock. Leave a fact '' when no
view shows it (views that show a sign-in or notice page instead do not count); never fill in 0 or a guess.
Report every price
condition record you see, older ones too, each with its own validity, currency, exchange-rate key, price unit,
declared number format and all of its quantity scales."""


def _want(req: EffectiveRequirement) -> str:
    stroke = f"stroke {req.stroke} mm" if req.stroke is not None else "stroke not yet known (ignore it)"
    return (f"bore {req.bore} mm, {stroke}, pressure {req.pressure} bar, mounting {req.mounting}, certification "
            f"{req.certification}")


def _num(text: str) -> Decimal | None:
    try:
        return parse_number(text) if text and text.strip() else None
    except ParseError:
        return None


def _material_record(seen: MaterialSeen) -> MaterialRecord:
    problems: list[str] = []

    def get(fn, text, *args):
        if not text or not str(text).strip():
            return None
        try:
            return fn(text, *args)
        except (ParseError, ValueError) as error:
            problems.append(str(error))
            return None

    conditions = []
    for c in seen.conditions:
        fmt = number_format(c.number_format) if c.number_format else None
        if not c.scales:
            problems.append(f"condition {c.valid_from}..{c.valid_to}: scales not read")
            continue
        try:
            scales = [calc.PriceScale(min_quantity=parse_int(s.min_quantity, fmt),
                                      price=parse_number(s.price, fmt)) for s in c.scales]
            conditions.append(calc.PriceCondition(
                valid_from=parse_date(c.valid_from), valid_to=parse_date(c.valid_to) if c.valid_to.strip() else None,
                currency=c.currency.strip().upper()[:3] or "EUR", fx_key=c.fx_key.strip() or None,
                price_unit=parse_int(c.price_unit), scales=scales))
        except (ParseError, ValueError) as error:
            problems.append(f"condition {c.valid_from}..{c.valid_to}: {error}")
    return MaterialRecord(
        material_number=seen.material_number.strip(), active=seen.active,
        status=f"{seen.status_code} ({seen.status_meaning})", bore=_num(seen.bore), stroke=_num(seen.stroke),
        pressure=_num(seen.pressure), mounting=seen.mounting.strip(), certification=seen.certification.strip(),
        free_stock=get(parse_int, seen.free_stock), blocked_stock=get(parse_int, seen.blocked_stock),
        stock_lead_time_weeks=get(parse_weeks, seen.stock_lead_time),
        production_lead_time_weeks=get(parse_weeks, seen.production_lead_time),
        pieces_per_carton=get(parse_int, seen.pieces_per_carton),
        allocation_mass_kg=get(parse_mass_kg, seen.allocation_mass),
        surcharge_mass_kg=get(parse_mass_kg, seen.surcharge_mass), metal_index=seen.metal_index.strip() or None,
        conditions=conditions, raw=seen.model_dump(), problems=problems)


def _keep_views(session: SiteSession, views: dict[str, str] | None = None) -> dict[str, str]:
    """Remember the latest text of every page this session shows (by URL), for the reader after navigation."""
    views = {} if views is None else views
    look = session.look

    async def recording_look(offset: int = 0, **options) -> str:  # brief/fresh pass through to look()
        out = await look(offset, **options)
        if offset == 0:
            views[session.page.url] = _strip_refs(session.view)
        return out

    session.look = recording_look  # type: ignore[method-assign]
    return views


def _corpus(views: dict[str, str]) -> str:
    """The kept views as one text, site chrome shown once."""
    texts = list(views.values())
    head, tail = _common_edges(texts)
    parts = []
    for i, (url, text) in enumerate(views.items()):
        lines = text.splitlines()
        if i and (head or tail):
            lines = lines[head:len(lines) - tail]
        parts.append(f"=== {url}\n" + "\n".join(lines))
    return "\n\n".join(parts)


async def _read_material(number: str, corpus: str, trace: Trace, name: str) -> MaterialSeen:
    return await _grounded_read(MaterialSeen, MATERIAL_INSTRUCTIONS, f"Material {number}. Its ERP views:\n\n{corpus}",
                                corpus, trace=trace, name=name, what="material views")


MAX_INSPECT = int(os.environ.get("ERP_MAX_INSPECT", "10"))


def incomplete(record: MaterialRecord) -> list[str]:
    """Facts every bid needs from a material that its record still lacks."""
    checks = {"status": bool(record.status.strip(" ()")), "free stock": record.free_stock is not None,
              "lead time from stock": record.stock_lead_time_weeks is not None,
              "production lead time": record.production_lead_time_weeks is not None,
              "price conditions with their quantity scales": bool(record.conditions)
              and not any("scales not read" in p for p in record.problems),
              "pieces per carton": record.pieces_per_carton is not None,
              "allocation and surcharge masses": record.allocation_mass_kg is not None
              and record.surcharge_mass_kg is not None}
    return [name for name, ok in checks.items() if not ok]


class ErpResearch(BaseModel):
    requirements: list[RequirementResearch] = Field(default_factory=list)
    materials: dict[str, MaterialRecord] = Field(default_factory=dict)
    seconds: float = 0


class Erp:
    """The ERP website: one signed-in context, a tab per worker, so requirements and materials run in parallel."""

    def __init__(self, site: dict, context: BrowserContext, trace: Trace, deadline: float | None = None):
        self.site, self.context, self.trace, self.deadline = site, context, trace, deadline
        self.ready: asyncio.Task | None = None
        self.materials: dict[str, asyncio.Task] = {}
        self.active_at = 0.0  # last time a worker was using the ERP
        self.busy = 0
        self.lock = asyncio.Lock()
        self.idle_limit = float(os.environ.get("ERP_IDLE_SECONDS", "200"))
        self.tasks: dict[str, asyncio.Task] = {}
        # a few tabs at once: when the ERP drops its session, every open tab signs in again at the same moment
        self.tabs = asyncio.Semaphore(int(os.environ.get("ERP_TABS", "8")))
        self.started_at: float | None = None

    def warm_up(self) -> asyncio.Task:
        """Sign in and get past entry gates once (a model handles gates the helpers cannot), e.g. while the
        specification is still being read."""
        if self.ready is None:
            self.ready = asyncio.create_task(self._warm_up())
        return self.ready

    async def _warm_up(self) -> None:
        session = await self._tab("start")
        await run_worker(session, "Get to the system's main menu or start screen, past any notice or confirmation "
                         "it asks for first, then return.", Ready, deadline=self.deadline, max_steps=12,
                         **_worker_kwargs(notes=False, grounded=False))
        await self._close(session)
        self.active_at = time.monotonic()

    async def _enter(self) -> None:
        """Before a worker starts: a tab slot, and the session signed in and not about to time out. When it sat
        idle for long (the ERP logs out after a few minutes), one tab signs in again before the others pile in."""
        await self.warm_up()
        await self.tabs.acquire()
        async with self.lock:
            if not self.busy and time.monotonic() - self.active_at > self.idle_limit:
                self.ready = None
                await self.warm_up()
            self.busy += 1

    def _leave(self) -> None:
        self.tabs.release()
        self.busy -= 1
        self.active_at = time.monotonic()

    async def _tab(self, tag: str, url: str | None = None) -> SiteSession:
        """A new tab in the shared context; `tag` only names it in the trace."""
        session = SiteSession({**self.site, "siteId": f"{self.site['siteId']}:{tag}"}, self.context, self.trace)
        await session.start()
        if url:
            await self.open(session, url)
        return session

    async def open(self, session: SiteSession, url: str) -> bool:
        """Open a link target a model reported, with the same rule as the goto tool: only on this website."""
        target = on_site(self.site, url)
        if target is None:
            self.trace.event(session.name, "link_refused", url=url)
            return False
        async with session.lock:
            await session._goto(target)
        await session.look()
        return True

    @staticmethod
    async def _close(session: SiteSession) -> None:
        for page in session.pages:
            try:
                await page.close()
            except Exception:  # already gone
                continue

    async def requirement(self, req: EffectiveRequirement) -> RequirementResearch:
        out = RequirementResearch(requirement_id=req.requirement_id)
        report = None
        for attempt in range(2):  # retry once: the ERP sometimes drops the session mid-search
            await self._enter()
            try:
                session = await self._tab(req.requirement_id if not attempt else f"{req.requirement_id}+")
                report, _ = await run_worker(session, SEARCH_GOAL.format(group=req.catalog_group, want=_want(req)),
                                             SearchReport, deadline=self.deadline, max_steps=14,
                                             **_worker_kwargs(notes=False))
                await self._close(session)
            finally:
                self._leave()
            if report is not None:
                break
        if report is None:
            out.problems.append("search worker returned no result")
            return out
        out.search_term = report.search_term
        out.candidates = [Candidate(material_number=c.material_number.strip(), link=c.link.strip(),
                                    bore=_num(c.bore), stroke=_num(c.stroke), pressure=_num(c.pressure),
                                    mounting=c.mounting, certification=c.certification)
                          for c in report.candidates]
        # every hit of the model's search is inspected (the rules cap a usable search at 20 hits); which one fits is
        # the model's choice below, once the details are known
        out.inspected = list(dict.fromkeys(c.material_number for c in out.candidates))[:MAX_INSPECT]
        if len(out.candidates) > MAX_INSPECT:
            out.problems.append(f"{len(out.candidates)} hits, inspected the first {MAX_INSPECT}")
        links = {c.material_number: c.link for c in out.candidates}
        records = await asyncio.gather(*(self.material(number, links.get(number, "")) for number in out.inspected))
        materials = {r.material_number: r for r in records if r is not None}
        out.selected, problems = await choose_material(req, out, materials, self.trace)
        out.problems += problems
        return out

    def material(self, number: str, link: str = "") -> asyncio.Task:
        """One detail worker per material, shared when several requirements inspect the same one."""
        if number not in self.materials:
            self.materials[number] = asyncio.create_task(self._material(number, link))
        return self.materials[number]

    async def _material(self, number: str, link: str) -> MaterialRecord | None:
        views: dict[str, str] = {}
        record = None
        focus = ""
        for attempt in range(2):  # a second pass, keeping what was read, when facts are missing
            await self._enter()
            try:
                session = await self._tab(number if not attempt else f"{number}+")
                _keep_views(session, views)
                if link:
                    await self.open(session, link)
                coverage, _ = await run_worker(session, DETAIL_GOAL.format(material=number, focus=focus), Coverage,
                                               deadline=self.deadline, max_steps=12, tools=(open_pages,),
                                               **_worker_kwargs(notes=False, grounded=False))
                await self._close(session)
            finally:
                self._leave()
            corpus = _corpus(views)
            self.trace.event(session.name, "material_read", views=len(views), chars=len(corpus),
                             not_found=coverage.not_found if coverage else "worker stopped")
            try:
                seen = await _read_material(number, corpus, self.trace, session.name)
            except Exception as error:
                self.trace.event(session.name, "material_failed", error=f"{type(error).__name__}: {str(error)[:200]}")
                continue
            record = _material_record(seen)
            missing = incomplete(record)
            if not missing:
                break
            record.problems.append(f"missing after pass {attempt + 1}: {', '.join(missing)}")
            focus = f"\nStill missing from the views read so far: {', '.join(missing)}. Open the views for these."
        if record is not None and record.material_number != number:
            record.problems.append(f"reader reported material {record.material_number} for {number}")
        return record

    def start(self, req: EffectiveRequirement) -> asyncio.Task:
        """Research one requirement in the background (e.g. as soon as the specification yields it)."""
        if self.started_at is None:
            self.started_at = time.monotonic()
        if req.requirement_id not in self.tasks:
            self.tasks[req.requirement_id] = asyncio.create_task(self.requirement(req))
        return self.tasks[req.requirement_id]

    async def collect(self) -> ErpResearch:
        """Wait for every started requirement and the materials they inspect."""
        results = [await self.tasks[k] for k in sorted(self.tasks, key=calc.position_key)]
        materials = {}
        for number, task in self.materials.items():
            record = await task
            if record is not None:
                materials[number] = record
        return ErpResearch(requirements=results, materials=materials,
                           seconds=round(time.monotonic() - (self.started_at or time.monotonic()), 1))

    async def research(self, requirements: list[EffectiveRequirement]) -> ErpResearch:
        for req in requirements:
            self.start(req)
        return await self.collect()


class MaterialChoice(BaseModel):
    material_number: str = Field(description="The chosen material number, or '' if none of them qualifies")
    why: str = Field(description="In a few words")


CHOOSE_MATERIAL = """\
You choose the ERP material for one tender requirement. Only an active (released) material qualifies, and its
bore, stroke, pressure, mounting and certification must all equal the requirement exactly (numbers as numbers;
no certification required means the material must name none). Blocked materials never qualify. If none
qualifies, answer ''."""


def _describe(number: str, record: MaterialRecord, research: RequirementResearch) -> str:
    attrs = merged_attributes(record, research)
    shown = ", ".join(f"{k} {attrs[k]}" for k in ("bore", "stroke", "pressure", "mounting", "certification")
                      if k in attrs)
    return f"- {number}: status {record.status}, active={record.active}; {shown}"


async def choose_material(req: EffectiveRequirement, research: RequirementResearch,
                          materials: dict[str, MaterialRecord], trace: Trace | None = None
                          ) -> tuple[str | None, list[str]]:
    """The model picks the material from the inspected candidates (thinking on); `evidence.check_choice` validates
    the exact match and status and sends a wrong choice back with the reason."""
    if req.stroke is None:
        return None, [f"{req.requirement_id}: stroke pending ({'; '.join(req.pending)})"]
    if not materials:
        return None, [f"{req.requirement_id}: no material could be inspected"]
    agent = Agent(llm.model(), output_type=MaterialChoice, instructions=CHOOSE_MATERIAL,
                  model_settings=_read_settings(True), retries=3)

    @agent.output_validator
    def _valid(out: MaterialChoice) -> MaterialChoice:
        problem = check_choice(req, research, materials, out.material_number.strip() or None)
        if problem:
            if trace:
                trace.event("choose", "choice_refused", requirement=req.requirement_id, chose=out.material_number,
                            problem=problem)
            raise ModelRetry(problem)
        return out

    listing = "\n".join(_describe(n, r, research) for n, r in materials.items())
    prompt = f"Requirement {req.requirement_id}: {_want(req)}.\nInspected ERP materials:\n{listing}"
    started = time.monotonic()
    try:
        choice = (await agent.run(prompt)).output
    except Exception as error:
        return None, [f"{req.requirement_id}: no valid choice ({type(error).__name__}: {str(error)[:120]})"]
    if trace:
        trace.event("choose", "material", requirement=req.requirement_id, chose=choice.material_number,
                    why=choice.why, seconds=round(time.monotonic() - started, 1))
    return choice.material_number.strip() or None, []


async def reselect(requirements: list[EffectiveRequirement], erp: ErpResearch,
                   trace: Trace | None = None) -> dict[str, str | None]:
    """Choose again once reserves or amendments have resolved the requirements (the ERP stage inspected every hit,
    so no further browsing is needed)."""
    by_id = {r.requirement_id: r for r in erp.requirements}

    async def one(req: EffectiveRequirement) -> str | None:
        research = by_id.get(req.requirement_id)
        if research is None:
            return None
        materials = {n: erp.materials[n] for n in research.inspected if n in erp.materials}
        return (await choose_material(req, research, materials, trace))[0]

    chosen = await asyncio.gather(*(one(r) for r in requirements))
    return {r.requirement_id: c for r, c in zip(requirements, chosen)}
