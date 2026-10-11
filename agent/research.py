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
import contextlib
import inspect
import os
import re
import time
from decimal import Decimal
from typing import Awaitable, Callable, TypeVar
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, Page
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

import calc
import llm
import site_tools
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
    group_without_hits,
    merged_attributes,
    mismatches,
    number_format,
    parse_currency,
    parse_date,
    parse_int,
    parse_mass_kg,
    parse_number,
    parse_pieces_per_set,
    parse_weeks,
)
from tracing import Trace
from worker import run_worker, steps_for

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

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
PAGE_WAIT = 2.0  # seconds read_document waits once more when a next-page click left the page unchanged


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


_LINK = re.compile(r"-> (\S+?)\]")


async def open_pages(ctx: RunContext[Deps], paths: list[str]) -> str:
    """Read several pages of this website in one step, e.g. the tabs, sub-views or records of what you are
    investigating. Give link targets exactly as pages you have seen show them (at most 10). The texts come back
    without refs, followed by the links on them you have not opened yet; you end up on the last one
    (read_more(offset=0) shows its refs)."""
    session = ctx.deps.session
    opened: set[str] = session.__dict__.setdefault("opened", set())  # link targets read in this session
    texts, names = [], []

    async def goto(target: str) -> str:
        async with session.lock:
            await session._goto(target)
            session.trace.event(session.name, "action", action="open_pages", url=target)
            return await session.look()

    def landed_on(target: str) -> bool:
        landed, wanted = urlparse(session.page.url), urlparse(target)
        return (landed.path, landed.query) == (wanted.path, wanted.query)

    for path in list(dict.fromkeys(paths))[:10]:
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
        if target in opened:
            texts.append("already opened and kept for the reader")
            names.append(path)
            continue
        view = await goto(target)
        recover = getattr(session, "recover", None)
        if not landed_on(target) and recover is not None:  # the session dropped: one tab gets past the gate
            await recover()
            view = await goto(target)
        if not landed_on(target):
            texts.append(f"did not open: the site showed {session.page.url} instead (handle that page, then open "
                         f"this view again)\n" + _strip_refs(view.split("\n\n", 1)[-1])[:3000])
            names.append(path)
            continue
        opened.add(target)
        texts.append(_strip_refs(view.split("\n\n", 1)[-1])[:8000])
        names.append(path)

    def was_opened(text: str) -> bool:
        return not text.startswith(("not opened", "did not open", "already opened"))

    head, tail = _common_edges([t for t in texts if was_opened(t)])
    out, more = [], {}
    for i, (name, text) in enumerate(zip(names, texts)):
        if was_opened(text):
            lines = text.splitlines()
            body = "\n".join(lines[head:len(lines) - tail]) if len(texts) > 1 else text  # not the site chrome
            for link in _LINK.findall(body):
                target = urljoin(session.page.url, link)
                if urlparse(target).netloc == urlparse(session.site["url"]).netloc and target not in opened:
                    more.setdefault(link, None)
        if i and was_opened(text) and (head or tail):
            lines = text.splitlines()
            text = "\n".join(lines[head:len(lines) - tail]) + "\n[page frame as in the first page]"
        out.append(f"=== {name}\n{text}")
    # links seen on any view this tool opened and not opened since, over all calls of this worker
    pending: dict[str, str] = session.__dict__.setdefault("pending_links", {})
    for link in more:
        pending.setdefault(urljoin(session.page.url, link), link)
    for target in list(pending):
        if target in opened:
            del pending[target]
    if more:
        out.append("Links on these views you have not opened yet: " + " ".join(list(more)[:40]))
    elif pending:
        out.append("Links on views you opened earlier that you have not opened yet: "
                   + " ".join(list(pending.values())[:40]))
    elif opened:
        # every link the opened views show has been read: the harness may end a worker that keeps going
        session.__dict__.setdefault("exhausted_at", time.monotonic())
        out.append("No unopened links remain: everything the views you opened link to has been opened. Return "
                   "your result now.")
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
            if text in seen:  # a slow site may still be loading the next page (a local run lost pages 3 to 5)
                await asyncio.sleep(PAGE_WAIT)
                await session.settle()
                await session.look()
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
    pieces_per_set: str = Field("", description="How many pieces one set (Satz) holds, if the text defines a set "
                                                 "(the piece count only, e.g. '4' for '1 set = 4 pieces'), else ''")
    stroke_is_usable_travel: bool = Field(description="True if the stroke is stated as usable travel (nutzbarer "
                                                      "Weg) to which a reserve must be added for the ERP stroke")
    reserve_key: str = Field("", description="The key of that end reserve, if named, else ''")
    conflict: str = Field("", description="Only if the table and the full wording disagree: how; else ''")


class SpecPage(BaseModel):
    revision_shown: str = Field(description="The revision this page belongs to, as shown on it, or ''")
    requirements: list[RequirementSeen]


FIND_SPEC_GOAL = """\
Find the buyer's technical specification document for the tender in the context, open its revision history and
return the revisions it lists. Steps: open the website's document register or list; read it with collect (all its
pages in one step) and pick the specification by the tender's reference or subject; open that document. If the
site asks for a confirmation before releasing documents, choose the option that gives access to the binding
documents (archive copies or previews are not the governing ones) and copy any number it asks you to transfer
exactly. On the document, open its revision history (it may be collapsed behind a control) and read it with collect;
return every revision it lists with its status as shown (statuses may only be visible in tooltips), so the revision
marked as currently valid can be identified. Once the revision history shows, return at once."""

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


SALVAGE_VIEWS = 12  # requirement pages read from what the open-revision worker showed, beyond read_document

SALVAGE_HISTORY = """\
You read the page a browsing worker ended on. If it shows the revision history of a specification document (a list
of its revisions with their statuses, which may only be visible in tooltips), report the document and every revision
with its status exactly as shown. If it shows no revision list, return an empty list of revisions."""


async def _salvage_history(session: SiteSession, context: str) -> RevisionHistory | None:
    """The revisions on the page a stopped navigator ended on (one grounded reader call), or None."""
    text = _strip_refs(session.view or "")
    if not text.strip():
        return None
    try:
        history = await _grounded_read(RevisionHistory, SALVAGE_HISTORY, f"{context}\n\nPage:\n{text[:16000]}", text,
                                       trace=session.trace, name=session.name, what="revision history salvage")
    except Exception as error:
        session.trace.event(session.name, "spec_salvage_failed", error=f"{type(error).__name__}: {str(error)[:160]}")
        return None
    return history if history.revisions else None


def _record_views(session: SiteSession) -> dict[str, str]:
    """Every distinct page text the session shows, latest last (a document viewer may page inside one URL)."""
    views: dict[str, str] = {}
    look = session.look

    async def recording_look(offset: int = 0, **options) -> str:
        out = await look(offset, **options)
        if offset == 0:
            text = _strip_refs(session.view)
            key = f"{session.page.url}#{hash(text)}"
            views.pop(key, None)
            views[key] = text
        return out

    session.look = recording_look  # type: ignore[method-assign]
    return views


def _pieces_per_set(text: str) -> int | None:
    """None when the spec gives no set size or words it unclearly (a set position is then left out, not mispriced)."""
    try:
        return parse_pieces_per_set(text) if text.strip() else None
    except ParseError:
        return None


def _parse_requirement(seen: RequirementSeen, revision: str) -> Requirement:
    return Requirement(
        requirement_id=seen.requirement_id.strip(), revision=revision, bore=parse_number(seen.bore),
        stroke=parse_number(seen.stroke), pressure=parse_number(seen.pressure), mounting=seen.mounting.strip(),
        certification=seen.certification.strip(), catalog_group=seen.catalog_group.strip(),
        pieces_per_set=_pieces_per_set(seen.pieces_per_set),
        stroke_is_usable=seen.stroke_is_usable_travel, reserve_key=seen.reserve_key.strip() or None,
        raw={k: str(v) for k, v in seen.model_dump().items()})


async def research_spec(session: SiteSession, briefing: Briefing, *, context: str = "",
                        deadline: float | None = None, on_requirement=None, on_revision=None,
                        thinking: bool = False, max_steps: int = 25) -> SpecResult:
    """
    The current specification revision's requirements: a worker finds the document and its revision history
    (navigation, thinking off), a model decides the current revision (thinking on), the worker opens it and pages
    through it, and a model extracts each page (thinking on, in parallel).

    `on_revision()` runs once the governing revision is chosen (e.g. to warm up the ERP), `on_requirement(req)` as
    soon as each requirement is parsed, so ERP research can start before the last page is read. `thinking`
    turns thinking on for finding the document, `max_steps` bounds that navigation (more while time is ample). A
    navigator that stops early still gets its revisions read from the page it ended on, and requirement pages it
    showed are read even when read_document was not used on them (partial requirements beat none).
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

    from site_tools import collect  # one step for a document register or a revision list
    views = _record_views(session)
    history, _ = await run_worker(session, FIND_SPEC_GOAL, RevisionHistory, context=goal_context, deadline=deadline,
                                  max_steps=steps_for(max_steps, deadline, most=1.4), tools=(collect,),
                                  **_worker_kwargs(notes=False, thinking=thinking))
    if history is None or not history.revisions:
        # The navigator ran out of steps or time, often while already on the history: read the revisions from the
        # page it ended on before giving up.
        history = await _salvage_history(session, goal_context)
        if history is None:
            return finish("no revision history found")
        session.trace.event(session.name, "spec_salvaged", revisions=[r.revision for r in history.revisions])
    result.document = history.document
    result.revisions = [SpecRevision(revision=r.revision, status_shown=r.status) for r in history.revisions]
    choice = await _choose_revision(history, session.view, goal_context)
    if choice is None:
        return finish("no current revision chosen")
    result.current_revision = choice.revision.strip()
    session.trace.event(session.name, "spec_revision", revision=result.current_revision, why=choice.why)
    if on_revision:
        on_revision()

    views.clear()  # from here on: the pages shown while the revision is opened
    pages: list[str] = []
    for _ in range(2):
        await run_worker(session, OPEN_REVISION_GOAL.format(revision=result.current_revision), Opened,
                         context=goal_context, deadline=deadline, max_steps=12, tools=(read_document,),
                         **_worker_kwargs(notes=False, grounded=False))
        pages = _DOCUMENTS.pop(id(session), [])
        if pages:
            break
    # Requirement pages the worker showed but read_document did not read (it stopped midway, or paged only part of
    # the document) are read too; they count only when the page itself shows the chosen revision, and only for
    # requirements the read pages do not have.
    extra = [v for v in list(views.values())[-SALVAGE_VIEWS:] if not any(v in p or p in v for p in pages)]
    if not pages and not extra:
        return finish(f"revision {result.current_revision} could not be read")
    session.trace.event(session.name, "spec_extract", pages=len(pages), salvage=len(extra))
    by_id: dict[str, Requirement] = {}
    streamed: set[str] = set()

    def stream(page: SpecPage | None, salvage: bool) -> None:
        if page is None or on_requirement is None:
            return
        shown = page.revision_shown.strip()
        if (shown and not same_revision(shown, result.current_revision)) or (salvage and not shown):
            return
        for seen in page.requirements:
            try:
                req = _parse_requirement(seen, result.current_revision)
            except ParseError:
                continue
            if req.requirement_id not in streamed:
                streamed.add(req.requirement_id)
                on_requirement(req)

    async def extract(text: str, salvage: bool = False) -> SpecPage | None:
        page = await _extract_page(text, history.document, result.current_revision, goal_context, session.trace,
                                   session.name)
        if not salvage:
            stream(page, False)
        return page

    extracted = await asyncio.gather(*(extract(p) for p in pages), *(extract(v, True) for v in extra))
    for i, page in enumerate(extracted):
        salvage = i >= len(pages)
        if salvage:  # after every read page, so a read page's requirement always wins
            stream(page, True)
        if page is None:
            if not salvage:
                result.problems.append("a document page could not be extracted")
            continue
        if page.revision_shown and not same_revision(page.revision_shown, result.current_revision):
            if not salvage:
                result.problems.append(f"a page shows revision {page.revision_shown}, not {result.current_revision}")
            continue
        if salvage and not page.revision_shown.strip():
            continue
        for seen in page.requirements:
            try:
                req = _parse_requirement(seen, result.current_revision)
            except ParseError as error:
                result.problems.append(f"{seen.requirement_id}: {error}")
                continue
            if seen.conflict:
                result.problems.append(f"{req.requirement_id}: {seen.conflict}")
            if req.requirement_id in by_id and by_id[req.requirement_id].raw != req.raw and not salvage:
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
List the ERP materials of catalogue group {group}. Open the ERP's material search (search help) and call
search(field_ref, query, submit_ref) on its search field with that group as the query: it types, submits and
returns every hit row with its links in one step. As soon as hits of that group show, return every one of them
with its link target and the technical attributes (bore, stroke, pressure, mounting, certification) the rows show,
whether or not they look like a fit: which material fits is decided later from their records, so never refine or
repeat a search that already lists hits of the group and do not open the hits. When search says "This is the
complete result list", you are done: return those hits right away (one hit is a complete answer too), without
collect, another query or paging. If the site refuses the search or shows no hits for the group, try the group in
another search field or spelling the site offers; when those show no hits either, return an empty list (searches
for other words do not tell which materials belong to the group). If a sign-in or notice page interrupts, get past
it."""

DETAIL_GOAL = """\
Open the views of ERP material {material} (you start on its record) that show these facts: its status and the
meaning of that status (a legend may sit in a tooltip); its technical data; its stock, down to the view that
separates freely usable from blocked or inspection stock; lead time from stock and production lead time; pieces
per carton, allocation weight, surcharge mass and metal/price index code; and EVERY price condition record with
its validity, currency, exchange-rate key, price unit and quantity scales (records may be paged one per page, and
scales may sit in a separate view of each record; a record list that says it has one record has no others).
Work in few steps: first call open_pages with every link of this record that leads to such a view (its tabs,
stock levels, condition records and their sub-views); its result lists the links on those views that you have not
opened yet, so open the ones that matter in one more open_pages call. Never guess addresses, and do not open a
view twice. If a sign-in, notice or error page interrupts, get past it and open again what it replaced. Every
view you open is kept for a reader, so you do not need to copy values: return as soon as all of these have been
opened, listing only facts you could not find (a fact a view shows as absent counts as found). When open_pages
says no unopened links remain, return at once: a fact that none of the opened views shows is not on this
record.{focus}"""

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
                currency=parse_currency(c.currency) or "EUR", fx_key=c.fx_key.strip() or None,
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


MAX_INSPECT = int(os.environ.get("ERP_MAX_INSPECT", "20"))  # the rules cap a usable search at 20 hits

# A worker that has what it came for (a complete hit list; every view of a record opened) but keeps going is ended
# this many seconds later; what it has is then read from its pages.
SETTLE_SECONDS = float(os.environ.get("ERP_SETTLE_SECONDS", "20"))

# Time the ERP stage keeps before the research deadline, so that what it started finishes and counts: a material's
# reader call after its worker stops (READ_SECONDS), the model's choice over the records (CHOOSE_SECONDS); a search
# starts only with time for a material after it (MATERIAL_SECONDS).
READ_SECONDS = 35.0
CHOOSE_SECONDS = 15.0
MATERIAL_SECONDS = 90.0
STEP_SECONDS = float(os.environ.get("ERP_STEP_SECONDS", "5"))  # one model step and its browser action, under load


UNFINISHED = "materials not all read by the research deadline"


def steps(until: float | None, floor: int, cap: int) -> int:
    """A worker's step limit: what the time until `until` allows at about STEP_SECONDS a step, between `floor`
    and `cap` (the cap only stops loops; the deadline stops the worker anyway)."""
    if until is None:
        return cap
    return int(max(floor, min(cap, (until - time.monotonic()) / STEP_SECONDS)))


GATE_GOAL = """\
Get past the sign-in, notice or confirmation page this system shows (copy any number it asks you to transfer
exactly) and return as soon as a page of the system itself shows (a main menu, transaction or material screen).
Open nothing else."""


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


SALVAGE_SEARCH = """\
You read the page a browsing worker ended on in an ERP. If it lists material hits of a search for catalogue group
{group}, report the search term shown and every listed hit of that group with its link target and the technical
attributes the list shows, exactly as written. If it shows no such hit list, return an empty list of candidates."""


async def _salvage_search(session: SiteSession, views: dict[str, str], group: str,
                          listing: str = "") -> SearchReport | None:
    """The hits of a stopped search worker (one grounded reader call): its complete hit list if it had one, else
    the latest page that lists the group with links; None without either."""
    text = listing or next((t for t in reversed(views.values()) if group in t and "->" in t), "")
    if not text:
        return None
    try:
        report = await asyncio.wait_for(_grounded_read(
            SearchReport, SALVAGE_SEARCH.format(group=group), f"Page:\n{text[:16000]}", text, trace=session.trace,
            name=session.name, what="search salvage"), 60)
    except Exception as error:
        session.trace.event(session.name, "search_salvage_failed", error=f"{type(error).__name__}: {str(error)[:160]}")
        return None
    return report if report.candidates else None


def contradicted(req: EffectiveRequirement, cand: Candidate) -> list[str]:
    """The attributes a hit's row shows that differ from the requirement as known now (attributes the row does not
    show, and a stroke still pending, contradict nothing)."""
    shown = {k: v for k, v in cand.attributes().items()
             if k not in ("mounting", "certification") or str(getattr(cand, k) or "").strip()}
    return [m for m in mismatches(req, shown) if not m.endswith(" not shown")]


def inspection_order(req: EffectiveRequirement, candidates: list[Candidate]) -> tuple[list[str], list[str]]:
    """The hits to inspect, at most MAX_INSPECT: first those whose rows fit the requirement as known now, then the
    others (a later amendment may still make one of them the match), each in list order."""
    fit: dict[str, None] = {}
    other: dict[str, None] = {}
    for cand in candidates:
        number = cand.material_number
        if number in fit or number in other:
            continue
        (other if contradicted(req, cand) else fit)[number] = None
    first = list(fit)[:MAX_INSPECT]
    return first, list(other)[:MAX_INSPECT - len(first)]


def _group_key(text: str) -> str:
    return re.sub(r"[\s\-_./]+", "", text).casefold()


def group_listing(searches: list[dict], group: str) -> dict | None:
    """From the searches a worker ran (as site_tools records them): the first complete hit list that shows the
    group."""
    key = _group_key(group)
    return next((s for s in searches if s.get("complete") and s.get("rows") and key in _group_key(s.get("text", ""))),
                None)


async def until_settled(work: Awaitable[R], settled: Callable[[], float | None], grace: float, trace: Trace,
                        name: str) -> tuple[R | None, bool]:
    """Await a worker; once `settled()` gives the time it got what it came for and `grace` seconds have passed
    without it returning, end it. Returns (its result or None, whether it was ended that way)."""
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                return task.result(), False
            at = settled()
            if at is not None and time.monotonic() - at >= grace:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
                trace.event(name, "settled_stop", grace=grace)
                return None, True
    finally:
        if not task.done():
            task.cancel()


class Erp:
    """
    The ERP website: one signed-in context and a few reused tabs, so requirements and materials run in parallel.

    The ERP drops its session after some idle minutes and when several tabs sign in at once. Sign-ins are therefore
    serialised over the tabs, and a tab that lands somewhere other than the view it asked for (a sign-in, an entry
    gate) gets one model worker past the gate (`recover`, one tab at a time) and opens its view again.
    """

    def __init__(self, site: dict, context: BrowserContext, trace: Trace, deadline: float | None = None):
        self.site, self.context, self.trace, self.deadline = site, context, trace, deadline
        self.ready: asyncio.Task | None = None
        self.materials: dict[str, asyncio.Task] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        # a few tabs at once: every tab is one more page load on a site that slows down under load
        self.tabs = asyncio.Semaphore(int(os.environ.get("ERP_TABS", "6")))
        self.idle_pages: list[Page] = []  # open tabs a finished worker left, reused instead of a fresh start
        self.opening = asyncio.Lock()  # fresh tabs sign in one after another
        self.signin = asyncio.Lock()  # one sign-in at a time
        self.signins = 0
        self.checking = asyncio.Lock()  # one idle check at a time
        self.gate = asyncio.Lock()  # one tab gets past an entry gate at a time
        self.passes = 0  # gates passed; a tab that waited for another one's pass just opens its view again
        self.gate_paths: set[str] = set()  # paths of the sign-in / notice gates seen
        self.home = ""  # the path the start address lands on while signed in and past the gate
        self.busy = 0  # workers using the ERP now
        self.active_at = 0.0  # last time a worker used it
        self.idle_limit = float(os.environ.get("ERP_IDLE_SECONDS", "120"))  # the ERP drops idle sessions
        self.partial: dict[str, RequirementResearch] = {}  # requirement -> research whose search has finished
        self.started_at: float | None = None

    def left(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - time.monotonic()

    def _until(self, reserve: float) -> float | None:
        return None if self.deadline is None else self.deadline - reserve

    def warm_up(self) -> asyncio.Task:
        """Sign in and get past entry gates once (a model handles gates the helpers cannot), e.g. while the
        specification is still being read."""
        if self.ready is None:
            self.ready = asyncio.create_task(self._warm_up())
        return self.ready

    async def _warm_up(self) -> None:
        async with self.gate:
            session = await self._worker_tab("start")
            if session is None:  # the workers' own tabs try again
                return
            try:
                await self._pass_gate(session)
            finally:
                await self._close(session)

    async def _pass_gate(self, session: SiteSession) -> None:
        """A model gets the tab past the sign-in / notice gate; then the start address shows where a signed-in
        tab lands (`home`). Called with `gate` held."""
        if not await self._at_home(session):  # the page it is on is a gate (a later drop may land there again)
            self.gate_paths.add(urlparse(session.page.url).path or "/")
        await run_worker(session, GATE_GOAL, Ready, deadline=self._until(0), max_steps=8,
                         **_worker_kwargs(notes=False, grounded=False))
        self.passes += 1
        async with session.lock:
            await session._goto(self.site["url"])
        await session.look()
        if not session.view.startswith("(") and not await self._signin_form(session):
            self.home = urlparse(session.page.url).path or "/"
        self.active_at = time.monotonic()

    @staticmethod
    async def _signin_form(session: SiteSession) -> bool:
        try:
            return bool(await session.page.locator("input[type=password]:visible").count())
        except Exception:
            return False

    async def _at_home(self, session: SiteSession) -> bool:
        return bool(self.home) and (urlparse(session.page.url).path or "/") == self.home \
            and not await self._signin_form(session)

    async def _at_gate(self, session: SiteSession) -> bool:
        return (urlparse(session.page.url).path or "/") in self.gate_paths or await self._signin_form(session)

    async def recover(self, session: SiteSession) -> None:
        """The tab did not reach its view (session dropped, entry gate): get past the gate once for all tabs."""
        if not await self._at_gate(session):  # e.g. a view that redirects elsewhere: not a dropped session
            return
        passes = self.passes
        async with self.gate:
            if self.passes != passes:  # another tab passed the gate while this one waited
                return
            self.trace.event(session.name, "erp_recover", url=session.page.url)
            await self._pass_gate(session)

    async def ensure(self) -> None:
        """Before a worker starts: the session signed in. After the ERP sat idle (it drops idle sessions), one tab
        checks it and passes the gate again if needed, before the other workers pile in and sign in at once."""
        await self.warm_up()
        if self.busy or time.monotonic() - self.active_at < self.idle_limit:
            return
        async with self.checking:
            if self.busy or time.monotonic() - self.active_at < self.idle_limit:
                return  # another worker checked meanwhile
            session = await self._worker_tab("check")  # a tab that does not land at home passes the gate again
            if session is not None:
                await self._close(session)
            self.active_at = time.monotonic()

    def _guard_login(self, session: SiteSession) -> None:
        """Sign-ins one tab at a time; a tab that waited while another signed in first reloads, since the shared
        context may already be signed in again."""
        login = session._login

        async def serialised(page: Page) -> bool:
            if not await page.locator("input[type=password]:visible").count():
                return False
            signins = self.signins
            async with self.signin:
                if self.signins != signins:
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    if not await page.locator("input[type=password]:visible").count():
                        session.events.append("another tab signed in; reloaded")
                        return True
                done = await login(page)
                if done:
                    self.signins += 1
                    await asyncio.sleep(1.0)  # let the sign-in land before the next tab acts
                return done

        session._login = serialised  # type: ignore[method-assign]

    async def _tab(self, tag: str, url: str | None = None) -> SiteSession:
        """A tab in the shared context (a reused one when a finished worker left it); `tag` names it in the
        trace. With `url`, the tab opens that link target, else the start address."""
        session = SiteSession({**self.site, "siteId": f"{self.site['siteId']}:{tag}"}, self.context, self.trace)
        self._guard_login(session)
        session.recover = lambda: self.recover(session)  # type: ignore[attr-defined]
        page = None
        while self.idle_pages and page is None:
            candidate = self.idle_pages.pop()
            page = None if candidate.is_closed() else candidate
        if page is not None:
            session.scripted = True  # the context already has the popup closer
            session._watch(page)
            if not url:
                async with session.lock:
                    await session._goto(self.site["url"])
                await session.look()
        else:
            async with self.opening:
                await session.start()
        if url:
            await self.open(session, url)
        elif self.home and not await self._at_home(session):  # dropped: one tab passes the gate for all
            await self.recover(session)
            async with session.lock:
                await session._goto(self.site["url"])
            await session.look()
        return session

    @contextlib.asynccontextmanager
    async def _slot(self):
        """A tab slot for one worker, with the session checked first."""
        await self.ensure()
        async with self.tabs:
            self.busy += 1
            try:
                yield
            finally:
                self.busy -= 1
                self.active_at = time.monotonic()

    async def open(self, session: SiteSession, url: str) -> bool:
        """Open a link target a model reported, with the same rule as the goto tool: only on this website. A tab
        that lands elsewhere (sign-in, entry gate) recovers once and opens it again."""
        target = on_site(self.site, url)
        if target is None:
            self.trace.event(session.name, "link_refused", url=url)
            return False
        for attempt in range(2):
            async with session.lock:
                await session._goto(target)
            await session.look()
            landed, wanted = urlparse(session.page.url), urlparse(target)
            if (landed.path, landed.query) == (wanted.path, wanted.query):
                session.__dict__.setdefault("opened", set()).add(target)
                return True
            if not attempt:
                await self.recover(session)
        return False

    async def _close(self, session: SiteSession) -> None:
        """Hand the worker's tab back for reuse (popups it opened are closed)."""
        kept = False
        for page in session.pages:
            try:
                page.remove_listener("dialog", session._on_dialog)
                page.remove_listener("popup", session._on_popup)
            except Exception:
                pass
            if not kept and not page.is_closed() and len(self.idle_pages) < 8:
                self.idle_pages.append(page)
                kept = True
                continue
            try:
                await page.close()
            except Exception:  # already gone
                continue

    async def _worker_tab(self, tag: str, url: str | None = None) -> SiteSession | None:
        try:  # a tab that does not open costs this attempt, not the research
            return await self._tab(tag, url)
        except Exception as error:
            self.trace.event("erp", "tab_failed", tag=tag, error=f"{type(error).__name__}: {str(error)[:160]}")
            return None

    async def _search(self, session: SiteSession, req: EffectiveRequirement, until: float | None,
                      searches: list) -> SearchReport | None:
        """The search worker, ended SETTLE_SECONDS after it has a complete hit list of the group; a worker that
        stops without a result leaves its hit list to the salvage reader."""
        views = _keep_views(session)
        session.__dict__["searches"] = searches  # the search tool records every search here

        def settled() -> float | None:
            """When the worker had its answer: a complete hit list of the group, or a second search for the group
            (another field or spelling) that the site also answered with no hits."""
            listing = group_listing(searches, req.catalog_group)
            if listing:
                return listing["at"]
            key = _group_key(req.catalog_group)
            empty = [s["at"] for s in searches if s.get("no_hits") and key in _group_key(s.get("query", ""))]
            return empty[1] if len(empty) > 1 else None

        result, _ = await until_settled(run_worker(
            session, SEARCH_GOAL.format(group=req.catalog_group), SearchReport, deadline=until,
            max_steps=steps(until, 6, 16), tools=(site_tools.search, site_tools.collect),
            **_worker_kwargs(notes=False)), settled, SETTLE_SECONDS, self.trace, session.name)
        report = result[0] if result else None
        if report is None:  # stopped (steps, time, settled): read the hit list it had
            listing = group_listing(searches, req.catalog_group)
            report = await _salvage_search(session, views, req.catalog_group, listing["text"] if listing else "")
        return report

    async def requirement(self, req: EffectiveRequirement) -> RequirementResearch:
        out = RequirementResearch(requirement_id=req.requirement_id)
        report = None
        until = self._until(MATERIAL_SECONDS)
        searches: list[dict] = []  # what the search worker's site_tools.search calls showed, over both attempts
        for attempt in range(2):  # retry once: the ERP sometimes drops the session mid-search
            if self.left() < MATERIAL_SECONDS + 20:
                break
            async with self._slot():
                session = await self._worker_tab(req.requirement_id if not attempt else f"{req.requirement_id}+")
                if session is None:
                    continue
                try:
                    report = await self._search(session, req, until, searches)
                finally:
                    await self._close(session)
            if report is not None or group_without_hits(searches, req.catalog_group):
                break  # a result, or the site said the group has no hits (another attempt would see the same)
        if (report is None or not report.candidates) and group_without_hits(searches, req.catalog_group):
            out.search_term = report.search_term if report else req.catalog_group
            out.no_hits = True  # searched, no match (rule 3), not a gap
            self.trace.event("erp", "group_without_hits", requirement=req.requirement_id,
                             searches=[{k: v for k, v in x.items() if k != "text"} for x in searches])
            return out
        if report is None:
            out.problems.append("search worker returned no result")
            return out
        out.search_term = report.search_term
        out.candidates = [Candidate(material_number=c.material_number.strip(), link=c.link.strip(),
                                    bore=_num(c.bore), stroke=_num(c.stroke), pressure=_num(c.pressure),
                                    mounting=c.mounting, certification=c.certification)
                          for c in report.candidates]
        # every hit of the model's search is inspected (the rules cap a usable search at 20 hits), those whose rows
        # fit the requirement as known now first; which one fits is the model's choice below, once details are known
        first, rest = inspection_order(req, out.candidates)
        out.inspected = first + rest
        hits = len(dict.fromkeys(c.material_number for c in out.candidates))
        if hits > len(out.inspected):
            out.problems.append(f"{hits} hits, inspected {len(out.inspected)}")
        # until the choice below is made, the research stands with its hits marked unfinished (so_far): a later
        # choice can still find the exact match among the materials read, and no match omits the position
        out.problems.append(UNFINISHED)
        self.partial[req.requirement_id] = out
        links = {c.material_number: c.link for c in out.candidates}
        tasks = {number: self.material(number, links.get(number, "")) for number in out.inspected}
        # material workers stop early enough for their reader; a record still open at the deadline is left out
        # and the choice is made over the ones that are complete. The hits whose rows fit come first: when one of
        # them is chosen, the others are not waited for (they still finish, for a later reselect).
        selected, problems, materials, phase = None, [], {}, out.inspected
        for phase in ([first, out.inspected] if first and rest else [out.inspected]):
            wanted = [tasks[n] for n in phase]
            done = set()
            if wanted:
                done, _ = await asyncio.wait(wanted, timeout=max(1.0, self.left() - CHOOSE_SECONDS))
            materials = {}
            for task in wanted:
                if task in done and not task.cancelled() and task.exception() is None and task.result() is not None:
                    materials[task.result().material_number] = task.result()
            try:
                selected, problems = await asyncio.wait_for(choose_material(req, out, materials, self.trace),
                                                            max(1.0, self.left() - 2))
            except asyncio.TimeoutError:
                selected, problems = None, [f"{req.requirement_id}: no choice before the research deadline"]
            if selected:
                break
        if len(materials) < len(phase):
            problems.append(f"{len(phase) - len(materials)} of {len(phase)} materials not read")
        out.selected = selected
        out.problems = [p for p in out.problems if p != UNFINISHED] + problems
        return out

    def so_far(self) -> ErpResearch:
        """What the ERP stage has at the deadline: finished requirements, the ones whose search finished (hits
        marked unfinished), and every finished material record."""
        results = []
        for k in sorted(self.tasks, key=calc.position_key):
            task = self.tasks[k]
            if task.done() and not task.cancelled() and task.exception() is None:
                results.append(task.result())
            elif k in self.partial:
                results.append(self.partial[k])
        return ErpResearch(requirements=results, materials=self.records(),
                           seconds=round(time.monotonic() - (self.started_at or time.monotonic()), 1))

    def material(self, number: str, link: str = "") -> asyncio.Task:
        """One detail worker per material, shared when several requirements inspect the same one."""
        if number not in self.materials:
            self.materials[number] = asyncio.create_task(self._material(number, link))
        return self.materials[number]

    async def _material(self, number: str, link: str) -> MaterialRecord | None:
        views: dict[str, str] = {}
        record = None
        focus = ""
        name = f"{self.site['siteId']}:{number}"
        for attempt in range(2):  # a second pass, keeping what was read, when facts are missing
            if self.left() < (READ_SECONDS + 30 if not attempt else MATERIAL_SECONDS + 30):
                break  # a second pass only with time for it; the first record stays either way
            coverage = None
            async with self._slot():
                session = await self._worker_tab(number if not attempt else f"{number}+", link or None)
                if session is None:
                    continue
                name = session.name
                try:
                    _keep_views(session, views)
                    if link and session.view:  # the record view it opened on, kept for the reader too
                        views[session.page.url] = _strip_refs(session.view)
                    until = self._until(READ_SECONDS)
                    # ended SETTLE_SECONDS after every view its pages link to is open: the reader reads them
                    result, _ = await until_settled(
                        run_worker(session, DETAIL_GOAL.format(material=number, focus=focus), Coverage,
                                   deadline=until, max_steps=steps(until, 6, 18), tools=(open_pages,),
                                   **_worker_kwargs(notes=False, grounded=False)),
                        lambda: session.__dict__.get("exhausted_at"), SETTLE_SECONDS, self.trace, session.name)
                    coverage = result[0] if result else None
                finally:
                    await self._close(session)
            corpus = _corpus(views)
            self.trace.event(name, "material_read", views=len(views), chars=len(corpus),
                             not_found=coverage.not_found if coverage else "worker stopped")
            try:
                seen = await asyncio.wait_for(_read_material(number, corpus, self.trace, name),
                                              max(5.0, self.left() - CHOOSE_SECONDS))
            except Exception as error:
                self.trace.event(name, "material_failed", error=f"{type(error).__name__}: {str(error)[:200]}")
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
        """Wait for every started requirement and the materials they inspect (each ends by the deadline)."""
        results = []
        for k in sorted(self.tasks, key=calc.position_key):
            try:
                results.append(await self.tasks[k])
            except Exception as error:  # one failed requirement must not cost the others
                results.append(RequirementResearch(requirement_id=k, problems=[
                    f"research failed: {type(error).__name__}: {str(error)[:160]}"]))
        return ErpResearch(requirements=results, materials=self.records(),
                           seconds=round(time.monotonic() - (self.started_at or time.monotonic()), 1))

    def records(self) -> dict[str, MaterialRecord]:
        """Every material record finished so far."""
        out = {}
        for number, task in self.materials.items():
            if not task.done() or task.cancelled():
                continue
            if task.exception() is not None:
                self.trace.event("erp", "material_failed", material=number, error=type(task.exception()).__name__)
            elif task.result() is not None:
                out[number] = task.result()
        return out

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

# The choice is a short decision over records already read, and `evidence.check_choice` sends a wrong one back with
# the reason, so it runs thinking off with a short timeout (RESEARCH_CHOOSE_THINKING=1 turns thinking on).
CHOOSE_THINKING = os.environ.get("RESEARCH_CHOOSE_THINKING", "0") == "1"


def _describe(number: str, record: MaterialRecord, research: RequirementResearch) -> str:
    attrs = merged_attributes(record, research)
    shown = ", ".join(f"{k} {attrs[k]}" for k in ("bore", "stroke", "pressure", "mounting", "certification")
                      if k in attrs)
    return f"- {number}: status {record.status}, active={record.active}; {shown}"


async def choose_material(req: EffectiveRequirement, research: RequirementResearch,
                          materials: dict[str, MaterialRecord], trace: Trace | None = None
                          ) -> tuple[str | None, list[str]]:
    """The model picks the material from the inspected candidates; `evidence.check_choice` validates the exact
    match and status and sends a wrong choice back with the reason."""
    if req.stroke is None:
        return None, [f"{req.requirement_id}: stroke pending ({'; '.join(req.pending)})"]
    if not materials:
        return None, [f"{req.requirement_id}: no material could be inspected"]
    agent = Agent(llm.model(), output_type=MaterialChoice, instructions=CHOOSE_MATERIAL,
                  model_settings=llm.settings(CHOOSE_THINKING, timeout=llm.env_seconds("LLM_CHOOSE_TIMEOUT", 30.0)),
                  retries=3)

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


RESELECT_SECONDS = float(os.environ.get("RESELECT_SECONDS", "50"))  # below the orchestrator's own cap


async def reselect(requirements: list[EffectiveRequirement], erp: ErpResearch, trace: Trace | None = None,
                   budget: float = RESELECT_SECONDS) -> dict[str, str | None]:
    """
    Choose again once reserves or amendments have resolved the requirements (the ERP stage inspected every hit,
    so no further browsing is needed). A choice the ERP stage's model made that still passes `check_choice` for the
    final requirement stands without another call; the others are asked in parallel within `budget` seconds. A
    requirement whose choice fails or runs out of time gets None and a problem on its research, so its position is
    left out rather than taken for "no material" (no_bid); the choices made by then are kept.
    """
    by_id = {r.requirement_id: r for r in erp.requirements}
    chosen: dict[str, str | None] = {}
    asked: list[str] = []

    async def one(req: EffectiveRequirement) -> None:
        research = by_id.get(req.requirement_id)
        if research is None:
            return
        if research.no_hits and not research.inspected:  # the group is empty: nothing to choose from
            chosen[req.requirement_id] = None
            return
        materials = {n: erp.materials[n] for n in research.inspected if n in erp.materials}
        if (req.stroke is not None and research.selected in materials
                and check_choice(req, research, materials, research.selected) is None):
            chosen[req.requirement_id] = research.selected
            return
        if req.stroke is not None:  # a stroke that was pending in the ERP stage is known now
            research.problems = [p for p in research.problems if "stroke pending" not in p]
        asked.append(req.requirement_id)
        number, problems = await choose_material(req, research, materials, trace)
        research.problems += [p for p in problems if p not in research.problems]
        chosen[req.requirement_id] = number

    started = time.monotonic()
    tasks = [asyncio.create_task(one(r)) for r in requirements]
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=budget)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for req in requirements:
        research = by_id.get(req.requirement_id)
        if research is not None and req.requirement_id not in chosen:
            research.problems.append(f"{req.requirement_id}: final material choice not made in time")
            chosen[req.requirement_id] = None
    if trace:
        trace.event("choose", "reselect", seconds=round(time.monotonic() - started, 1), asked=asked, chosen=chosen)
    return chosen
