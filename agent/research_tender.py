"""
Tender research: the tender's positions with their original quantities, and everything the bidder Q&A changes.

A navigation worker (thinking off, so steps are fast) finds the tender on the bid website, picks the documents
that hold the bill of quantities and hands their links to `keep_pages`, which opens them and keeps their text. On
the bidder Q&A it hands the list's "next page" (or "load more") control to `collect_pages`, which pages through
that model-selected result set. Two readers (thinking on) then turn the kept pages into typed records, checked
against the pages, and `resolve_tender` applies the mechanical rules (latest dated answer per position and key,
withdrawn answers ignored, nothing after the scenario date) in calc's terms.
"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext

import llm
from browser_tools import Deps, SiteSession, _click
from calc import (
    Amendment,
    QuantityChange,
    Unit,
    parse_unit,
    position_key,
    resolve_quantity,
)
from evidence import ParseError, parse_int, parse_number
from worker import run_worker, steps_for

MAX_PAGES = 40
MAX_RETURN = 30000  # characters of a collected list shown back to the model
READER_TIMEOUT = 180.0  # a thinking reader writes long answers; the navigation timeout is too short

# ---------------------------------------------------------------- records


class TenderPosition(BaseModel):
    position: str = Field(description="Position number exactly as written, e.g. with its dots and leading zeros")
    short_text: str = Field(description="The position's short text")
    quantity: str = Field(description="ORIGINAL quantity from the tender documents, in the stated unit, exactly as written (digits and separators unchanged, without unit)")
    unit: str = Field(description="Unit exactly as written")
    requirement: str = Field(description="Requirement id the position refers to in the buyer's specification")


class TenderPositions(BaseModel):
    positions: list[TenderPosition]
    number_format: str = Field(default="", description="'de' (decimal comma), 'en' (decimal point) or '' unclear")


class Summary(BaseModel):
    summary: str = Field(description="One or two sentences on what you kept")


class QaRef(BaseModel):
    answer_number: str = Field(description="The answer's number (Nr.) exactly as shown")
    date: datetime = Field(description="The answer's date as an ISO 8601 timestamp; prefer the UTC value the page "
                                       "shows (ending in Z), else the local time with its offset")


class QaQuantityChange(QaRef):
    position: str
    quantity: str = Field(description="The new total quantity, in the position's stated unit, exactly as written (digits and separators unchanged, without unit)")
    unit: str = Field(default="", description="Unit the answer states, if any")


class QaWithdrawal(QaRef):
    withdrawn_number: str = Field(description="Number of the earlier answer this answer withdraws (zurückziehen)")


class QaKey(QaRef):
    position: str
    key: str = Field(description="The key exactly as written, e.g. a group, tariff or plan identifier")


class QaSetupFee(QaRef):
    position: str
    amount: str = Field(description="The ordinary one-off setup fee for the whole position (net EUR), "
                                    "exactly as written (digits and separators unchanged, without unit)")


AttributeName = Literal["bore", "stroke", "pressure", "mounting", "certification", "pieces_per_set", "other"]


class QaAmendment(QaRef):
    revision: str = Field(description="Specification revision the amendment names, exactly as written")
    requirement: str = Field(description="Requirement id the amendment names")
    attribute: AttributeName = Field(description="Which attribute it changes: bore, stroke (including a "
                                                 "usable stroke / nutzbarer Weg), pressure, mounting, certification, "
                                                 "pieces_per_set, or other")
    attribute_label: str = Field(description="The attribute's name as written")
    value: str = Field(description="The new value as written, without its unit")
    unit: str = Field(default="", description="The value's unit, if any (e.g. mm, bar)")


class QaRecords(BaseModel):
    """Every fact the bidder Q&A states, one record per answer and fact (an answer can carry several)."""
    number_format: str = Field(default="", description="The number format the page uses: 'de' (decimal comma), "
                                                       "'en' (decimal point) or '' if unclear")
    quantity_changes: list[QaQuantityChange] = Field(default_factory=list)
    withdrawals: list[QaWithdrawal] = Field(default_factory=list)
    condition_groups: list[QaKey] = Field(default_factory=list, description="Konditionsgruppe per position")
    packaging_tariffs: list[QaKey] = Field(default_factory=list, description="Packaging tariff key per position")
    delivery_commitments: list[QaKey] = Field(default_factory=list,
                                              description="Delivery commitment (Termin) key per position")
    setup_pools: list[QaKey] = Field(default_factory=list,
                                     description="Shared setup pool (Rüstpool) a position is a member of")
    project_bonuses: list[QaKey] = Field(default_factory=list,
                                         description="Project bonus plan a position is a member of")
    setup_fees: list[QaSetupFee] = Field(default_factory=list)
    amendments: list[QaAmendment] = Field(default_factory=list,
                                          description="Technical amendments, one record per changed attribute")


class PositionFacts(BaseModel):
    """One position after the Q&A rules, ready to map onto calc.PositionInput."""
    position: str
    short_text: str
    requirement: str
    unit: Unit
    unit_text: str
    tender_quantity: int
    quantity: int = Field(description="Final quantity in the stated unit (rules 1, 13)")
    quantity_answer: str | None = Field(default=None, description="Q&A answer number that set it, None = tender")
    condition_group: str | None = None
    packaging_key: str | None = None
    commitment_key: str | None = None
    setup_pool_key: str | None = None
    bonus_key: str | None = None
    setup_fee: Decimal = Decimal(0)


class TenderFacts(BaseModel):
    positions: list[PositionFacts]
    amendments: list[Amendment] = Field(default_factory=list, description="For calc.apply_amendments (rule 14)")
    amendment_units: dict[str, str] = Field(default_factory=dict, description="attribute -> unit as written")
    withdrawn: list[str] = Field(default_factory=list)
    qa: QaRecords
    seconds: float = 0.0


# ---------------------------------------------------------------- the paging helper


REF_LINE = re.compile(r'\[(e\d+) (\w+) "([^"]*)"')
REFS = re.compile(r"\be\d+\b")
REF = re.compile(r"e\d+")
LINK = re.compile(r'\[(e\d+) link "([^"]*)" -> ([^\]\s]+)\]')
EMPTY_FRAME = re.compile(r"\[frame ([^\]\s]+)\]\n\[/frame\]")


def _normal(view: str) -> str:
    return REFS.sub("", view)


@dataclass
class Collected:
    pages: list[str] = field(default_factory=list)

    def add(self, view: str) -> bool:
        """Keep a page view; False when it adds nothing new (past the end a list repeats a page it showed)."""
        new = _normal(view)
        earlier = [_normal(p) for p in self.pages]
        if new in earlier:
            return False
        if earlier and set(earlier[-1].splitlines()) <= set(new.splitlines()):  # "load more" grew the list
            self.pages[-1] = view
            return True
        self.pages.append(view)
        return True

    @property
    def text(self) -> str:
        return "\n\n".join(f"=== page view {i + 1}\n{p}" for i, p in enumerate(self.pages))


def _control(view: str, ref: str) -> tuple[str, str] | None:
    for r, kind, name in REF_LINE.findall(view):
        if r == ref:
            return kind, name
    return None


def paging_tool(collected: Collected):
    """A tool that pages through the result set the model is looking at, by repeating the control it names."""

    async def collect_pages(ctx: RunContext[Deps], next_ref: str) -> str:
        """Collect the whole list you are looking at: give the ref of its "next page" or "load more" control. The
        control is clicked until the list ends; every page is kept for reading afterwards. Call it once, on the
        list's first page."""
        session: SiteSession = ctx.deps.session
        control = _control(session.view, next_ref)
        if control is None:
            return f"No control {next_ref} in the latest view; use a ref from the latest view."
        kind, name = control
        collected.add(session.view)
        ref, pages = next_ref, 1
        for _ in range(MAX_PAGES):
            before = _normal(session.view)
            await session.act("click", ref, _click)
            for _ in range(4):  # a list inside a frame can update a moment after the click settled
                if _normal(session.view) != before:
                    break
                await asyncio.sleep(0.7)
                await session.look()
            if not collected.add(session.view):
                break
            pages += 1
            same = [r for r, k, n in REF_LINE.findall(session.view) if k == kind and n == name]
            if not same:
                break
            ref = same[0]
        session.trace.event(session.name, "collect_pages", control=name, pages=len(collected.pages))
        session.trace.event(session.name, "collected", quiet=True, pages=collected.pages)
        text = collected.text
        if len(text) > MAX_RETURN:
            text = text[:MAX_RETURN] + f"\n[... {len(text) - MAX_RETURN} more characters, kept for reading]"
        return f"Collected {pages} page(s) of this list; they are kept for reading. The whole list:\n\n{text}"

    return collect_pages


async def open_and_keep(session: SiteSession, urls: list[str], kept: list[str]) -> list[str]:
    """Open each chosen URL in a temporary tab of the session's browser context, side by side, and keep its text."""

    async def one(url: str) -> str:
        tab = SiteSession(session.site, session.context, session.trace)
        page = await session.context.new_page()
        tab._watch(page)
        try:
            await tab._goto(url)
            view = await tab.look()
            # An embedded document that did not render in place (e.g. a lazy reading pane) is part of the chosen
            # page: open it on its own and add its text.
            for src in EMPTY_FRAME.findall(view)[:3]:
                await tab._goto(urljoin(url, src))
                view += f"\n[frame {src} opened]\n" + await tab.look()
            return view
        finally:
            await page.close()

    views = await asyncio.gather(*(one(u) for u in urls), return_exceptions=True)
    good = [v for v in views if isinstance(v, str)]
    for view in good:
        kept.append(view)
        session.seen += "\n" + view
    return good


def _heads(views: list[str]) -> str:
    return "\n".join("- " + " / ".join(v.splitlines()[:2]) for v in views)


def keep_tool(kept: list[str]):
    """A tool that opens link targets the model chose, each in a temporary tab, and keeps their page text."""

    async def keep_pages(ctx: RunContext[Deps], targets: list[str]) -> str:
        """Open these links and keep each page for reading afterwards: give each link's target (the "-> ..." part)
        or its ref from the latest view. Pass every page you want at once; they open side by side. Your own tab
        stays where it is."""
        session: SiteSession = ctx.deps.session
        host = urlparse(session.site["url"]).netloc

        def on_a_page(target: str) -> bool:  # chosen from a view, not made up
            url = urlparse(urljoin(session.page.url, target))
            local = url.path + (f"?{url.query}" if url.query else "")
            return target in session.seen or f"-> {local}" in session.seen or f"-> {url.geturl()}" in session.seen

        links = LINK.findall(session.view)
        hrefs = {ref: href for ref, _, href in links}  # a link's ref from the latest view stands for its target
        targets = [hrefs.get(t.strip(), t.strip()) for t in targets]
        shown = [t for t in targets if t and not REF.fullmatch(t) and on_a_page(t)]
        unknown = [t for t in targets if t.strip() and t.strip() not in shown]
        urls = list(dict.fromkeys(urljoin(session.page.url, t) for t in shown))
        urls = [u for u in urls if urlparse(u).netloc == host][:25]
        good = await open_and_keep(session, urls, kept)
        session.trace.event(session.name, "keep_pages", targets=urls, unknown=unknown, kept=len(good))
        answer = f"Kept {len(good)} of {len(urls)} page(s):\n{_heads(good)}"
        if unknown:
            listing = "\n".join(f"  {ref}: {href}" for ref, _, href in links[:40])
            answer += (f"\nNot opened, these targets appear on no page you have seen: {unknown[:10]}. Use link "
                       f"targets exactly as the views show them. Links in the latest view:\n{listing}")
        return answer

    return keep_pages


def _clean(text: str) -> str:
    return " ".join(text.split()).casefold()


def keep_results_tool(kept: list[str]):
    """A tool that opens the result links whose visible text the model names, from every list the worker saw."""

    async def keep_results(ctx: RunContext[Deps], link_texts: list[str]) -> str:
        """Open every result link whose visible text is exactly one of `link_texts` (e.g. a message subject as a
        result list shows it), from all result lists you have seen (all folders and pages), and keep each opened
        page for reading. Copy the texts exactly: a text with any extra characters is a different record."""
        session: SiteSession = ctx.deps.session
        host = urlparse(session.site["url"]).netloc
        wanted = {_clean(t) for t in link_texts if t.strip()}
        hits = [href for _, text, href in LINK.findall(session.seen) if _clean(text) in wanted]
        urls = list(dict.fromkeys(urljoin(session.site["url"], h) for h in hits))
        urls = [u for u in urls if urlparse(u).netloc == host][:20]
        good = await open_and_keep(session, urls, kept)
        session.trace.event(session.name, "keep_results", texts=link_texts, urls=urls, kept=len(good))
        if not urls:
            texts = sorted({text for _, text, _ in LINK.findall(session.view)})[:40]
            return (f"No link seen with exactly these texts: {link_texts}. Link texts in the latest view: {texts}")
        return f"Kept {len(good)} of {len(urls)} result page(s):\n{_heads(good)}"

    return keep_results


def _goal(reference: str) -> str:
    return f"""For tender {reference} (exactly this reference; similar references are other tenders):
1. Open this tender and its list of documents. Call keep_pages once with the link targets of the documents that
   can hold the positions (bill of quantities / position list with the original quantities, and the description).
   Ignore buttons and links that lead elsewhere (surveys, tools). Do not open the documents yourself.
2. Open the tender's bidder questions and answers (Q&A). When the list of answers shows, call collect_pages with
   the ref of the list's control for the next page (or for loading more). Do not page through it yourself.
3. Then return a short summary. Do not open or start the bid submission. To move between pages follow links or
   goto a link target; do not use back(). You need not note() anything: the kept
   pages are read for you."""


def _positions_goal(reference: str) -> str:
    return f"""For tender {reference} (exactly this reference; similar references are other tenders): open this
tender and its list of documents. Call keep_pages once with the link targets of the documents that can hold the
positions (bill of quantities / position list with the original quantities, and the description). Ignore buttons
and links that lead elsewhere (surveys, tools). Do not open the documents yourself, and do not open the bidder
questions and answers or the bid submission: another worker reads those. Then return a short summary. To move
between pages follow links or goto a link target; do not use back()."""


def _qa_goal(reference: str) -> str:
    return f"""For tender {reference} (exactly this reference; similar references are other tenders): open this
tender's bidder questions and answers (Q&A). When the list of answers shows, call collect_pages with the ref of
the list's control for the next page (or for loading more); do not page through it yourself. Do not open the
tender's documents or the bid submission: another worker reads those. Then return a short summary. To move between
pages follow links or goto a link target; do not use back()."""


POSITIONS_INSTRUCTIONS = """\
You read the documents of a public tender and list its positions exactly as written: position number,
short text, quantity, unit and the requirement id of the buyer's specification the position refers to. Use the
ORIGINAL quantities the tender documents were published with; later changes are not part of this. If several documents list the positions, they should agree; prefer a table of the bill of
quantities. List every position once."""


QA_INSTRUCTIONS = """\
You read the bidder Q&A of a public tender and record every fact it states, exactly as written.
The answers may be shown out of number order (sorted by date); each answer has a number and a date. Use the
answer's own date (prefer the UTC timestamp shown, e.g. in a tooltip). One answer can carry several facts: record
each of them, with that answer's number and date. Record what the answers say; do not resolve conflicts, do not
skip older or superseded answers, and do not invent facts for positions an answer does not name. Statements that
a position is NOT a member of a pool or plan mean no membership: record nothing for them. An answer that withdraws
(zieht zurück) an earlier answer is a withdrawal of that earlier answer's number. Technical amendments name a
specification revision and a requirement: record one amendment per changed attribute. Copy numbers exactly as
written; do not convert them. The same answer can appear on two page views; record it once. Ignore page furniture (navigation, notices, buttons)."""


def _numbers(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for token in re.findall(r"\d[\d.,]*", text):
        token = token.rstrip(".,")
        for candidate in (token.replace(",", "."), token.replace(".", "").replace(",", "."), token.replace(",", "")):
            try:
                found.add(Decimal(candidate))
            except InvalidOperation:
                pass
    return found


def ungrounded(record: BaseModel, text: str, skip: tuple[str, ...] = ("date",)) -> list[str]:
    """Values in a record (numbers, keys) that the source text does not contain; dates are not checked."""
    numbers = _numbers(text)
    flat = " ".join(text.split())
    missing: list[str] = []

    def check(value, name: str) -> None:
        if name in skip or value is None or isinstance(value, (bool, datetime)):
            return
        if isinstance(value, BaseModel):
            for k, v in value:
                check(v, k)
        elif isinstance(value, (list, tuple)):
            for v in value:
                check(v, name)
        elif isinstance(value, dict):
            for k, v in value.items():
                check(v, str(k))
        elif isinstance(value, (int, Decimal, float)):
            if Decimal(str(value)) not in numbers:
                missing.append(f"{name}={value}")
        elif isinstance(value, str) and any(c.isdigit() for c in value):
            if " ".join(value.split()) not in flat and not (_numbers(value) and _numbers(value) <= numbers):
                missing.append(f"{name}={value!r}")

    check(record, "")
    return missing


def reader_model():
    """The run's model; readers get their longer deadline per call from reader_settings()."""
    return llm.model()


def reader_settings():
    """Thinking on, with a whole-call deadline long enough for readers that write long typed answers."""
    return llm.settings(thinking=True, timeout=READER_TIMEOUT)


async def _read(output_type, instructions: str, text: str, prompt: str):
    agent = Agent(reader_model(), output_type=output_type, instructions=instructions,
                  model_settings=reader_settings(), retries=3)

    @agent.output_validator
    def _grounded(records):
        missing = ungrounded(records, text)
        if missing:
            raise ModelRetry(f"These values are not in the pages: {', '.join(missing[:12])}. Copy values exactly.")
        return records

    return (await agent.run(f"{prompt}\n\n{text}")).output


async def read_positions(text: str, reference: str) -> TenderPositions:
    return await _read(TenderPositions, POSITIONS_INSTRUCTIONS, text, f"Tender {reference}. Documents:")


async def read_qa(pages: list[str], reference: str) -> QaRecords:
    """Each page view read on its own, in parallel (short answers, fast), then merged without duplicates."""
    parts = await asyncio.gather(*(_read(QaRecords, QA_INSTRUCTIONS, page, f"Tender {reference}. Bidder Q&A page:")
                                   for page in pages))
    merged = QaRecords(number_format=next((p.number_format for p in parts if p.number_format), ""))
    for name in QaRecords.model_fields:
        if name == "number_format":
            continue
        seen: dict[str, BaseModel] = {}
        for part in parts:
            for record in getattr(part, name):
                seen.setdefault(record.model_dump_json(), record)
        setattr(merged, name, list(seen.values()))
    return merged


async def research_tender(session: SiteSession, reference: str, scenario: datetime, *,
                          deadline: float | None = None, qa_session: SiteSession | None = None,
                          on_facts=None) -> TenderFacts | None:
    """
    Positions and Q&A of the tender, resolved per position. None when no positions were found.

    With `qa_session` (a second tab of the website) the documents and the Q&A are worked side by side, each by its
    own worker. `on_facts(facts)` gets the positions with their original quantities as soon as they are read, so a
    deadline that comes before the Q&A still leaves them; the returned facts include the Q&A.
    """
    started = time.monotonic()
    if qa_session is None:
        return await _research_tender_one(session, reference, scenario, deadline=deadline, on_facts=on_facts)
    collected = Collected()
    documents: list[str] = []

    async def positions_part() -> TenderPositions:
        await run_worker(session, _positions_goal(reference), Summary, deadline=deadline,
                         max_steps=steps_for(14, deadline), thinking=False, grounded=False,
                         tools=(keep_tool(documents),))
        return await read_positions("\n\n".join(documents or [session.seen]), reference)

    async def qa_part() -> QaRecords:
        await run_worker(qa_session, _qa_goal(reference), Summary, deadline=deadline,
                         max_steps=steps_for(14, deadline), thinking=False, grounded=False,
                         tools=(paging_tool(collected),))
        return await read_qa(collected.pages or [qa_session.seen], reference)

    qa_task = asyncio.create_task(qa_part())
    try:
        try:
            positions = await positions_part()
        except Exception as error:
            session.trace.event(session.name, "tender_positions_failed", error=f"{type(error).__name__}: {error}"[:200])
            positions = TenderPositions(positions=[])
        if not positions.positions:  # the Q&A worker passed the tender's pages too
            await asyncio.wait([qa_task])
            try:
                positions = await read_positions(qa_session.seen, reference)
            except Exception:
                positions = TenderPositions(positions=[])
        if not positions.positions:
            session.trace.event(session.name, "tender_failed")
            return None
        fmt = positions.number_format or None
        if on_facts is not None and not qa_task.done():
            early = resolve_tender(positions.positions, QaRecords(), scenario, fmt)
            session.trace.event(session.name, "tender_positions", seconds=round(time.monotonic() - started, 1),
                                positions=[p.position for p in early.positions])
            on_facts(early)
        try:
            qa = await qa_task
        except Exception as error:
            session.trace.event(session.name, "tender_qa_failed", error=f"{type(error).__name__}: {error}"[:200])
            qa = QaRecords()
    finally:
        if not qa_task.done():
            qa_task.cancel()
    facts = resolve_tender(positions.positions, qa, scenario, fmt)
    facts.seconds = round(time.monotonic() - started, 1)
    session.trace.event(session.name, "tender_facts", seconds=facts.seconds,
                        positions=[p.model_dump(mode="json") for p in facts.positions])
    if on_facts is not None:
        on_facts(facts)
    return facts


async def _research_tender_one(session: SiteSession, reference: str, scenario: datetime, *,
                               deadline: float | None = None, on_facts=None) -> TenderFacts | None:
    """Positions and Q&A by one worker in one tab."""
    started = time.monotonic()
    collected = Collected()
    documents: list[str] = []
    _, notes = await run_worker(session, _goal(reference), Summary, deadline=deadline,
                                max_steps=steps_for(20, deadline), thinking=False, grounded=False,
                                tools=(paging_tool(collected), keep_tool(documents)))
    if not documents:  # the worker read the documents in its own tab instead
        documents = [session.seen]
    qa_pages = collected.pages or [session.seen]  # without collect_pages: the answers it saw itself
    positions, qa = await asyncio.gather(read_positions("\n\n".join(documents), reference),
                                         read_qa(qa_pages, reference))
    if not positions.positions:
        session.trace.event(session.name, "tender_failed", notes=notes)
        return None
    facts = resolve_tender(positions.positions, qa, scenario, positions.number_format or None)
    facts.seconds = round(time.monotonic() - started, 1)
    session.trace.event(session.name, "tender_facts", seconds=facts.seconds,
                        positions=[p.model_dump(mode="json") for p in facts.positions])
    if on_facts is not None:
        on_facts(facts)
    return facts


# ---------------------------------------------------------------- the mechanical rules


def _same_ref(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def _latest_key(records: list[QaKey], position: str, withdrawn: set[str], scenario: datetime) -> str | None:
    live = [r for r in records if _same_ref(r.position, position) and r.answer_number not in withdrawn
            and _aware(r.date) <= scenario]
    return max(live, key=lambda r: (_aware(r.date), position_key(r.answer_number))).key if live else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _decimal_or_text(value: str, fmt: str | None) -> Decimal | str:
    try:
        return parse_number(value, fmt) if re.fullmatch(r"\s*[-+]?\d[\d.,'\s]*", value) else value.strip()
    except ParseError:
        return value.strip()


def resolve_tender(positions: list[TenderPosition], qa: QaRecords, scenario: datetime,
                   positions_format: str | None = None) -> TenderFacts:
    """Rules 1, 13, 14 and the Q&A keys: latest dated answer per position and key, withdrawn ones ignored."""
    scenario = _aware(scenario)
    withdrawn = {w.withdrawn_number for w in qa.withdrawals if _aware(w.date) <= scenario}
    fmt = qa.number_format or None
    changes = [QuantityChange(number=c.answer_number, date=_aware(c.date), position=c.position,
                              quantity=parse_int(c.quantity, fmt))
               for c in qa.quantity_changes if _aware(c.date) <= scenario]
    facts = []
    for p in sorted(positions, key=lambda p: position_key(p.position)):
        mine = [c for c in changes if _same_ref(c.position, p.position)]
        for c in mine:
            c.position = p.position
        original = parse_int(p.quantity, positions_format)
        final = resolve_quantity(original, p.position, mine, withdrawn, scenario)
        live = [c for c in mine if c.number not in withdrawn]
        source = max(live, key=lambda c: (c.date, position_key(c.number))).number if live else None
        fees = [f for f in qa.setup_fees if _same_ref(f.position, p.position) and f.answer_number not in withdrawn
                and _aware(f.date) <= scenario]
        fee = parse_number(max(fees, key=lambda f: _aware(f.date)).amount, fmt) if fees else Decimal(0)
        facts.append(PositionFacts(
            position=p.position, short_text=p.short_text, requirement=p.requirement, unit=parse_unit(p.unit),
            unit_text=p.unit, tender_quantity=original, quantity=final, quantity_answer=source,
            condition_group=_latest_key(qa.condition_groups, p.position, withdrawn, scenario),
            packaging_key=_latest_key(qa.packaging_tariffs, p.position, withdrawn, scenario),
            commitment_key=_latest_key(qa.delivery_commitments, p.position, withdrawn, scenario),
            setup_pool_key=_latest_key(qa.setup_pools, p.position, withdrawn, scenario),
            bonus_key=_latest_key(qa.project_bonuses, p.position, withdrawn, scenario),
            setup_fee=fee))
    amendments = [Amendment(date=_aware(a.date), revision=a.revision, requirement=a.requirement,
                            attributes={a.attribute if a.attribute != "other" else a.attribute_label:
                                        _decimal_or_text(a.value, fmt)})
                  for a in qa.amendments if a.answer_number not in withdrawn and _aware(a.date) <= scenario]
    units = {a.attribute: a.unit for a in qa.amendments if a.unit}
    return TenderFacts(positions=facts, amendments=amendments, amendment_units=units, withdrawn=sorted(withdrawn),
                       qa=qa)
