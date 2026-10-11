"""
The v2 orchestrator: a planner decides which facts the bid needs and which website each comes from, research
workers post what they find on the findings board, dependent workers start as soon as their inputs exist, a
verifier checks the facts against the bid rules, calc turns them into the answer, and a deadline guard hands
whatever is complete to the bid form in time.

Flow (every arrow is a board event, not a fixed order in code):
  brief -> plan -> tender Q&A (positions, quantities, keys, amendments)
                -> specification (requirements, streamed) -> ERP per requirement -> reselect() once amendments
                   and reserves are known
                -> mails (approval, rules, indices, reserves, tariffs, commitments, pools, bonus)
  all facts -> verifier (doubtful facts go back to their worker once) -> calc -> decision confirmed -> answer

The tender Q&A and mail research modules are optional imports: without one the pipeline runs on the rest
(spec + ERP + calc), and fields that need its facts stay blank. The mail side has its own deadline. Nothing here names a site, a label or a value of any dataset.
"""
import asyncio
import inspect
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from playwright.async_api import BrowserContext
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry

import calc
import llm
from board import Board, Source
from briefing import Briefing
from browser_tools import SiteSession
from evidence import (EffectiveRequirement, Requirement, effective_requirement, merged_attributes, mismatches,
                      parse_date, to_position_input, wording_differences)
from gaps import Calculation, GapFiller
from research import Erp, ErpResearch, SpecResult, research_spec, reselect, same_revision
from tracing import Trace
from verify import VERIFY_SECONDS, confirm_decision, verify_board, wording_check

try:  # the pipeline degrades without them
    import research_tender
except ImportError:  # pragma: no cover
    research_tender = None
try:
    import research_mail
except ImportError:  # pragma: no cover
    research_mail = None

# research ends this long before the end: the form needs up to ~185 s and signing ~30 s (173 s with a bad TAN)
SUBMIT_SECONDS = float(os.environ.get("AGENT_SUBMIT_SECONDS", "390"))
FORM_SECONDS = float(os.environ.get("AGENT_FORM_SECONDS", "300"))  # the bid form and signing need this much
MAIL_TABS = int(os.environ.get("MAIL_TABS", "4"))
SPEC_STEPS_FAST = int(os.environ.get("SPEC_STEPS_FAST", "25"))  # navigation steps of the fast spec attempt
SPEC_STEPS_THINKING = int(os.environ.get("SPEC_STEPS_THINKING", "35"))  # and of the careful one, run alongside
SELECT_SECONDS = 75.0  # the final material choice (a model call per requirement, in parallel)
KIND_OPTIONS = set(inspect.signature(research_mail.research_kind).parameters) if research_mail else set()
KIND_MIN_SECONDS = 120.0  # a mail record kind's search budget: at least this
KIND_MAX_SECONDS = 240.0  # and at most this, from the mail time left per kind
MAIL_RETRY_SECONDS = 90.0  # mail time a kind's second try needs
MAIL_TAB_STAGGER = 4.0  # seconds between the mail tabs' sign-ins
MAIL_SECONDS = float(os.environ.get("AGENT_MAIL_SECONDS", "540"))  # from the orchestrator's start
# How long the mail planner waits for the tender's keys before planning from the brief alone. The keys arrive with
# the Q&A (about 120 s in) while the wait was 45 s, so every sandbox run planned from the brief alone after waiting:
# the wait only held the mail searches, the last research to finish, back by 45 s. Positions already on the
# board are still passed along.
FINDINGS_WAIT = float(os.environ.get("AGENT_FINDINGS_WAIT", "0"))
PLAN_PATIENT_TIMEOUT = 90.0  # per call when the quick mail plan failed: a loaded model answers in up to ~90 s
# doubtful mail facts go back to their search in the gap pass's first round (side by side with the gap retries)
# instead of in a round of their own before calc, which held the first answer back by up to 2 minutes
GAP_REWORK = os.environ.get("AGENT_GAP_REWORK", "1") == "1"
VERIFY_ALONGSIDE = os.environ.get("AGENT_VERIFY_ALONGSIDE", "1") == "1"  # see run()
REWORK_SECONDS = 150.0
TENDER_WATCH_SECONDS = float(os.environ.get("AGENT_TENDER_WATCH_SECONDS", "240"))  # no positions by then: retry
RECHECK_SECONDS = float(os.environ.get("AGENT_RECHECK_SECONDS", "150"))  # the Q&A re-check before a no_bid
TENDER_RETRY_SECONDS = 150.0  # research time a second tender attempt needs  # a doubtful fact goes back to its worker only with at least this much research time left


async def start_session(session: SiteSession, until: float | None = None) -> SiteSession:
    """Open and sign in (SiteSession.start: waits on a slow tab, retries with backoff until `until`)."""
    await session.start(until=until)
    return session

# ---------------------------------------------------------------- the planner

FACT_KINDS = {
    "tender": "the tender itself: its positions with quantities and units, and the bidder questions and answers "
              "(quantity changes, withdrawn answers, scoped technical amendments, and per-position keys for "
              "condition groups, packaging, delivery commitments, setup pools and bonus plans)",
    "specification": "the buyer's technical specification: which revision is current and the technical "
                     "requirement (attributes, catalogue group, pieces per set) per requirement id",
    "materials": "the bidder's own ERP: the materials of a catalogue group with status, stock, lead times, "
                 "pieces per carton, masses, metal index code and price conditions with quantity scales",
    "mails": "authority records in the bidder's own mailbox: discount approval, condition rules, metal index "
             "rates, stroke reserves, packaging tariffs, delivery commitments, exchange rates, setup pools, "
             "bonus plans",
}


class Need(BaseModel):
    kind: Literal[tuple(FACT_KINDS)] = Field(description="Which facts")  # type: ignore[valid-type]
    site: str = Field(description="siteId of the website that holds them")
    why: str = Field(description="One sentence: which bid rules need these facts")


class Plan(BaseModel):
    needs: list[Need] = Field(description="Every kind of fact the bid needs (each kind at least once), each with "
                                          "its website")


def complete_plan(plan: Plan, briefing: Briefing, ids: set[str]) -> tuple[Plan, list[str]]:
    """
    A plan that still lacks a fact kind after the model's retries: the tender goes to the bid website and the mails
    to the mailbox (both read from the brief); a missing specification or ERP goes to every website that holds no
    other kind. Returns the completed plan and what was filled in.
    """
    have = {n.kind for n in plan.needs}
    free = sorted(ids - {n.site for n in plan.needs} - {briefing.bid_site, briefing.mail_site})
    needs, filled = list(plan.needs), []
    for kind in FACT_KINDS:
        if kind in have:
            continue
        for site in [briefing.bid_site] if kind == "tender" else [briefing.mail_site] if kind == "mails" else free:
            if site in ids:
                needs.append(Need(kind=kind, site=site, why="missing from the model's plan; filled in"))
                filled.append(f"{kind}@{site}")
    return Plan(needs=needs), filled


async def plan_research(task: dict, briefing: Briefing) -> Plan:
    """
    One model call: from the brief, which facts the bid needs and which website each comes from. Every fact kind
    is needed (the rules use the tender, the specification, the ERP and the mails), so a plan that drops one is
    sent back; one still incomplete after the retries is asked once more without that check and completed by
    complete_plan().
    """
    ids = {s["siteId"] for s in task["access"]}
    sites = "\n".join(f"- siteId {s['siteId']}: {s['url']}" for s in task["access"])
    kinds = "\n".join(f"- {k}: {v}" for k, v in FACT_KINDS.items())
    agent = Agent(llm.model(), output_type=Plan, model_settings=llm.settings(llm.thinking("plan", False)), retries=3,
                  instructions="You plan the research for a tender bid from its brief: which kinds of facts the "
                               "bid rules need, and on which of the run's websites each kind is found. Use the "
                               "siteIds exactly.")
    strict = [True]

    @agent.output_validator
    def _known(plan: Plan) -> Plan:
        bad = sorted({n.site for n in plan.needs} - ids)
        if bad:
            raise ModelRetry(f"{bad} are not siteIds of this run: {sorted(ids)}")
        missing = [k for k in FACT_KINDS if k not in {n.kind for n in plan.needs}]
        if missing and strict[0]:
            raise ModelRetry(f"The bid rules need every kind of fact; {missing} have no website yet. Add each "
                             f"with the website that holds it.")
        return plan

    brief = task["brief"]
    prompt = (f"Websites of this run:\n{sites}\n\nKinds of facts the workers can establish:\n{kinds}\n\n"
              f"The bid is entered on {briefing.bid_site}; the bidder's mailbox is {briefing.mail_site}.\n\n"
              f"Brief:\n{brief}")
    try:
        return (await agent.run(prompt)).output
    except Exception as error:
        print(f"[plan] no complete plan ({type(error).__name__}): asked again, gaps filled in", flush=True)
    strict[0] = False
    try:
        plan = (await agent.run(prompt)).output
    except Exception:
        plan = Plan(needs=[])
    plan, filled = complete_plan(plan, briefing, ids)
    print(f"[plan] filled in {filled}", flush=True)
    return plan


# ---------------------------------------------------------------- small helpers


def scenario_time(text: str) -> datetime:
    """The scenario date and time as an aware datetime; a bare date counts until its end (rule 21: "no later")."""
    if re.search(r"\d:\d\d", text):  # a bare ISO date would parse as midnight, the start of the day
        try:
            value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        except ValueError:
            pass
    day = parse_date(text)
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        return datetime(day.year, day.month, day.day, int(m.group(1)), int(m.group(2)), tzinfo=UTC)
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1) - timedelta(seconds=1)


def rules_text(brief: str) -> str:
    m = re.search(r"^#+\s*(Rules|Regeln|Bid rules|Bietregeln)\b.*$", brief, re.IGNORECASE | re.MULTILINE)
    return brief[m.start():] if m else brief


def quote_for(seen: str, *tokens: str, width: int = 300) -> str:
    """The first line of what a worker saw that carries every token: the fact's quote for the verifier."""
    wanted = [t for t in tokens if t]
    for line in seen.splitlines():
        if wanted and all(t in line for t in wanted):
            return line.strip()[:width]
    return ""


def _same_id(a: str, b: str) -> bool:
    a, b = " ".join(str(a).split()).casefold(), " ".join(str(b).split()).casefold()
    return a == b or (bool(a) and bool(b) and (a.endswith(b) or b.endswith(a)))


# ---------------------------------------------------------------- the calculation with per-field fallbacks


def safe_calculate(bid: calc.BidInput) -> tuple[calc.BidResult | None, dict[str, set[str]], list[str]]:
    """
    calc with CalcError caught per position: a position whose packaging or surcharge inputs are incomplete is
    priced without them and those report fields (and its unit price) are left blank; one that cannot be priced
    at all is omitted. Returns the result, the blank fields per position and what was dropped.
    """
    positions = list(bid.positions)
    blank: dict[str, set[str]] = {}
    problems: list[str] = []
    for _ in range(3 * len(positions) + 3):
        try:
            return calc.calculate(bid.model_copy(update={"positions": positions})), blank, problems
        except calc.CalcError as error:
            message, named = str(error), error.position
        # an error that names its position (e.g. a lead time only the pooled demand needs) is that position's
        culprit, why = next((p for p in positions if named is not None and p.position == named), None), message
        for p in positions if culprit is None else ():
            try:
                calc.calculate(bid.model_copy(update={"positions": [p]}))
            except calc.CalcError as error:
                culprit, why = p, str(error)
                break
        if culprit is None:  # a bid-level input (pool, bonus) fails for the set as a whole
            pools, bonuses = bid.all_setup_pools(), bid.all_project_bonuses()
            if pools:  # the last pool first: one bad key must not cost the others theirs
                bid = bid.model_copy(update={"setup_pool": None, "setup_pools": pools[:-1]})
                problems.append(f"setup pool {pools[-1].key or ''} dropped: {message}")
            elif bonuses:
                bid = bid.model_copy(update={"project_bonus": None, "project_bonuses": bonuses[:-1]})
                problems.append(f"project bonus {bonuses[-1].key or ''} dropped: {message}")
            else:
                problems.append(f"calc failed: {message}")
                return None, blank, problems
            continue
        k = culprit.position
        fields = blank.setdefault(k, set())
        if culprit.packaging is not None and "carton" in why:
            replacement = culprit.model_copy(update={"packaging": None})
            fields |= {"packaging_per_unit", "unit_price"}
        elif culprit.material is not None and culprit.material.surcharge_mass and ("metal" in why or "index" in why):
            material = culprit.material.model_copy(update={"surcharge_mass": Decimal(0)})
            replacement = culprit.model_copy(update={"material": material})
            fields |= {"surcharge_per_unit", "unit_price"}
        else:
            positions = [p for p in positions if p is not culprit]
            problems.append(f"position {k} omitted: {why}")
            continue
        problems.append(f"position {k}: {why}; field left blank")
        positions = [replacement if p is culprit else p for p in positions]
    problems.append("calc gave up")
    return None, blank, problems


def material_attributes(material, requirement=None) -> dict[str, str]:
    """
    The chosen material's attributes as the ERP shows them, completed by the requirement's (its catalogue group
    and whatever the ERP did not show): the bid form's cascading choices lead through any of them to the material.
    """
    out = {}
    for key in ("catalog_group", "bore", "stroke", "pressure", "mounting", "certification"):
        for source in (material, requirement):
            value = getattr(source, key, None)
            if value not in (None, ""):
                out[key] = format(value, "f") if isinstance(value, Decimal) else str(value)
                break
    return out


def answer_from(result: calc.BidResult, blank: dict[str, set[str]], notes: str = "") -> dict:
    """
    The answer with the report fields whose inputs are missing left blank. The unit price stays: it is calc's best
    estimate without the missing part, and the bid form enters a position only with a unit price, so blanking it
    would lose the position's material, quantity, base price, packaging and delivery points too.
    """
    answer = result.answer(notes or None)
    for position in answer["positions"]:
        for field in blank.get(position["position"], ()):
            if field != "unit_price":
                position.pop(field, None)
    return answer


# ---------------------------------------------------------------- the orchestrator


class Orchestrator:
    def __init__(self, task: dict, briefing: Briefing, contexts: dict[str, BrowserContext], trace: Trace,
                 deadline: float, warm: dict[str, SiteSession] | None = None):
        self.task, self.briefing, self.contexts, self.trace = task, briefing, contexts, trace
        self.warm = warm or {}  # per website the session agent.py is signing in with, maybe still starting
        self.access = {s["siteId"]: s for s in task["access"]}
        self.deadline = deadline
        self.research_deadline = deadline - SUBMIT_SECONDS
        self.board = Board(trace)
        self.reference = briefing.tender_reference
        self.scenario = scenario_time(briefing.scenario_date)
        self.scenario_day: date = self.scenario.date()
        # calc compares two timestamps as instants and anything date-only by day: hand it the brief's own grain.
        self.scenario_stamp: date | datetime = (self.scenario if re.search(r"\d:\d\d", briefing.scenario_date)
                                                else self.scenario_day)
        self.rules = rules_text(task["brief"])
        self.plan: Plan | None = None
        self.tender_facts = None
        self.tender_reads: dict[str, object] = {}  # one entry per tender read (website, retry, re-check): combined
        self.tender_calls = 0
        self.mail_facts = None
        self.spec: SpecResult | None = None
        self.erp: ErpResearch | None = None
        self._erp: Erp | None = None
        self.requirements: dict[str, Requirement] = {}
        self.spec_revision: str | None = None  # the revision the requirements so far come from
        self.chosen: dict[str, str | None] = {}
        self.mail_kinds: list = []
        self.mail_sessions: list[SiteSession] = []
        self.rework: dict[str, object] = {}  # worker -> coroutine function for doubtful facts
        self.gap_rework = GAP_REWORK  # doubtful mail kinds are retried in the gap pass (gaps.py)
        self.blank_hints: dict[str, set[str]] = {}  # position -> answer fields that stay blank
        self.attributes: dict[str, dict[str, str]] = {}  # position -> chosen material's attributes, for the form
        self.mailbox = ""
        self.mail_results: dict[str, object] = {}  # record kind -> research_mail.KindResult
        # The mail side is the slowest (a planner and about nine record kinds): it gets its own deadline, and
        # calc runs with whatever records exist by then.
        self.mail_deadline = min(self.research_deadline, time.monotonic() + MAIL_SECONDS)

    def left(self) -> float:
        return self.research_deadline - time.monotonic()

    def form_left(self) -> float:
        """Seconds until the bid form must start."""
        return self.deadline - FORM_SECONDS - time.monotonic()

    async def _bounded(self, step, name: str) -> None:
        try:
            await asyncio.wait_for(step, max(1.0, self.form_left()))
        except Exception as error:
            self.board.problem(name, f"stopped: {type(error).__name__} (the bid form needs the time)")

    async def site_up(self, site: str) -> None:
        """
        Wait until the website's first session (agent.py's, signing in in the background) is up, so workers join
        a website when it comes up instead of hammering a slow one with more tabs; the others go on meanwhile.
        Raises when that start gave up or the research deadline passes first.
        """
        warm = self.warm.get(site)
        if warm is None or warm.ok:
            return
        if not warm.settled.is_set() or warm.starting:
            self.trace.event(site, "waiting_for_site", seconds_left=round(self.left()))
            try:
                await asyncio.wait_for(warm.settled.wait(), max(1.0, self.left()))
            except asyncio.TimeoutError:
                raise TimeoutError(f"{site}: not up by the research deadline") from None
            self.trace.event(site, "site_up" if warm.ok else "site_down")
        if not warm.ok:
            raise TimeoutError(f"{site}: the site could not be opened")

    async def session(self, site: str, tag: str = "") -> SiteSession:
        access = self.access[site]
        if tag:
            access = {**access, "siteId": f"{site}:{tag}"}
        await self.site_up(site)
        return await start_session(SiteSession(access, self.contexts[site], self.trace), until=self.research_deadline)

    # ------------------------------------------------------------ workers

    async def _tender_watch(self, site: str, first: asyncio.Task) -> None:
        """
        The gap check that cannot wait for the end of research: every position, and so the whole answer, waits on
        the tender's positions. When none are read TENDER_WATCH_SECONDS after the tender research started (a
        reader call that keeps timing out held them for ten minutes in one sandbox run), a second tender attempt
        on fresh tabs runs alongside; whichever completes with positions first wins and the other is stopped.
        """
        started = time.monotonic()

        def read() -> bool:
            return bool(self.board.get("position")) or bool(getattr(self.tender_facts, "positions", None))

        # An attempt that ended without positions (soak run 83438e79: its worker hit the request limit at 58 s) is
        # retried at once, not only after the whole research.
        while not read() and not first.done() and time.monotonic() - started < TENDER_WATCH_SECONDS:
            await asyncio.sleep(min(2.0, max(0.01, TENDER_WATCH_SECONDS - (time.monotonic() - started))))
        if read() or self.left() < TENDER_RETRY_SECONDS:
            return
        self.trace.event("gaps", "gap", gap="tender/positions", reason="no positions read yet", early=True,
                         seconds=round(time.monotonic() - started))
        retry = asyncio.create_task(self.tender(site, "2"), name=f"tender2@{site}")
        pending = {first, retry}
        try:
            while pending:
                _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if getattr(self.tender_facts, "positions", None):
                    break
        finally:  # also at the research deadline: the retry is no job of research(), so it ends here
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if getattr(self.tender_facts, "positions", None):
            self.trace.event("gaps", "gap_filled", gap="tender/positions", early=True,
                             by="retry" if retry.done() and not retry.cancelled() else "first attempt")

    async def tender(self, site: str, attempt: str = "") -> None:
        board = self.board
        try:
            session, qa_session = await asyncio.gather(self.session(site, f"tender{attempt}"),
                                                       self.session(site, f"qa{attempt}"), return_exceptions=True)
            if isinstance(session, BaseException):
                if isinstance(qa_session, BaseException):
                    raise session
                session, qa_session = qa_session, None
            elif isinstance(qa_session, BaseException):
                board.problem("tender", f"no second tab ({type(qa_session).__name__}); one worker reads all")
                qa_session = None

            self.tender_calls += 1
            read = f"{site}#{self.tender_calls}{attempt}"

            def on_facts(facts) -> None:
                self._tender_read(read, facts, site, session)

            facts = await research_tender.research_tender(session, self.reference, self.scenario,
                                                          deadline=self.research_deadline, qa_session=qa_session,
                                                          on_facts=on_facts)
            if facts is None:
                board.problem("tender", f"{site}: neither positions nor Q&A found")
                return
            if self.tender_reads.get(read) is not facts:  # in case the module did not report it
                on_facts(facts)
            if not facts.positions:
                board.problem("tender", f"{site}: no positions found (its Q&A is kept)")
        except asyncio.CancelledError:
            if self.tender_facts is not None:
                board.problem("tender", "stopped at the deadline: positions without the full Q&A are used")
            raise
        except Exception as error:
            board.problem("tender", f"{type(error).__name__}: {str(error)[:200]}")
        finally:
            for kind in ("position", "amendment", "withdrawn"):
                board.finish(kind)

    def _tender_read(self, read: str, facts, site: str, session: SiteSession) -> None:
        """
        One tender read (a website's positions and Q&A, a retry, a re-check) joins the others: positions from the
        read that has them, Q&A answers from all (research_tender.combine). Two websites' reads used to overwrite
        each other, so Q&A on another website than the bill of quantities was lost.
        """
        self.tender_reads[read] = facts
        reads = list(self.tender_reads.values())
        combine = getattr(research_tender, "combine", None)
        self.tender_facts = combine(reads, self.scenario) if combine is not None and len(reads) > 1 else facts
        self._post_tender(self.tender_facts, site, session)

    def _post_tender(self, facts, site: str, session: SiteSession) -> None:
        board = self.board
        for p in facts.positions:
            value = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in p.model_dump(mode="json").items()}
            board.post("position", p.position, value,
                       Source(site=site, url=session.page.url,
                              quote=quote_for(session.seen, p.position, str(p.tender_quantity))
                              or quote_for(session.seen, p.position)), "tender")
        for a in getattr(facts, "amendments", []):
            key = f"{a.revision}/{a.requirement}/{'+'.join(a.attributes)}"
            board.post("amendment", key, {"date": a.date.isoformat(), **{k: str(v) for k, v in a.attributes.items()}},
                       Source(site=site, quote=quote_for(session.seen, a.requirement,
                                                         *map(str, a.attributes.values()))), "tender")
        for number in getattr(facts, "withdrawn", []):
            board.post("withdrawn", number, number, Source(site=site, quote=quote_for(session.seen, number)),
                       "tender")

    def _amendments(self) -> list[calc.Amendment]:
        return list(getattr(self.tender_facts, "amendments", []) or [])

    def _reserves(self, revision: str) -> dict[str, Decimal]:
        """Per-end reserves by reserve key, only from records for the requirement's own spec revision (they are
        revision-specific); a record that names no revision counts for any, after one for this revision. The mail
        facts hold one entry per key and revision (entry.key is the reserve key)."""
        reserves = getattr(self.mail_facts, "end_reserves", None) or {}
        own: dict[str, Decimal] = {}
        unnamed: dict[str, Decimal] = {}
        for name, entry in reserves.items():
            key = getattr(entry, "key", "") or name
            value = Decimal(str(getattr(entry, "reserve_per_end_mm", entry)))
            rev = getattr(entry, "revision", "")
            if not rev:
                unnamed[key] = value
            elif calc.same_label(rev, revision) or _same_id(rev, revision):
                own[key] = value
        return {**unnamed, **own}

    def effective(self, req: Requirement) -> EffectiveRequirement:
        return effective_requirement(req, amendments=self._amendments(), reserves=self._reserves(req.revision),
                                     scenario_date=self.scenario_stamp)

    async def specification(self, site: str, erp_site: str | None) -> None:
        """
        The specification, twice at once: a fast navigator (thinking off) and a careful one (thinking on, more
        steps) work in separate contexts of the website; the first that yields a revision with requirements wins
        and the other is cancelled. Requirements stream to the ERP as soon as either attempt reads them, all from
        one revision (the first one read); requirements read before an attempt stopped are kept.
        """
        board = self.board
        erp = None
        if erp_site:
            if self._erp is None:
                self._erp = Erp(self.access[erp_site], self.contexts[erp_site], self.trace, self.research_deadline)
                self._erp.warm_up()
            erp = self._erp
        spare = None
        try:
            context = self.task["brief"].split("## Rules")[0].strip()
            spare = await self._spare_context(site)
            attempts = {
                asyncio.create_task(self._spec_attempt(site, "spec", self.contexts[site], context, erp,
                                                       thinking=False, steps=SPEC_STEPS_FAST)): "fast",
                asyncio.create_task(self._spec_attempt(site, "spec2", spare or self.contexts[site], context, erp,
                                                       thinking=True, steps=SPEC_STEPS_THINKING)): "thinking",
            }
            results: list[SpecResult] = []
            pending = set(attempts)
            while pending and self.spec is None:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled() or task.exception() is not None:
                        error = None if task.cancelled() else task.exception()
                        board.problem("specification", f"{attempts[task]} attempt failed: {type(error).__name__}: "
                                                       f"{str(error)[:160]}")
                        continue
                    result = task.result()
                    results.append(result)
                    if result.current_revision and result.requirements and self.spec is None:
                        self.spec = result
                        self.trace.event("specification", "winner", attempt=attempts[task],
                                         revision=result.current_revision, seconds=result.seconds)
                    else:
                        for problem in result.problems:
                            board.problem("specification", f"{attempts[task]} attempt: {problem}")
            for task in pending:  # the other attempt is no longer needed
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self._close_context(spare)  # its tabs would compete with the ERP workers for the browser
            spare = None
            if self.spec is None:  # neither finished with requirements: keep the most that was read
                self.spec = max(results, key=lambda r: len(r.requirements), default=SpecResult())
            self._adopt_revision(self.spec, site, erp)
            for problem in self.spec.problems:
                board.problem("specification", problem)
            if not self.requirements:
                board.problem("specification", "no requirement read")
            if erp is None:
                return
            self.erp = await erp.collect()
            await self._post_materials(erp_site or "")
        except Exception as error:
            board.problem("specification", f"{type(error).__name__}: {str(error)[:200]}")
        finally:
            board.finish("requirement")
            board.finish("material")
            await self._close_context(spare)

    @staticmethod
    async def _close_context(context: BrowserContext | None) -> None:
        if context is not None:
            try:
                await context.close()
            except Exception:  # already gone
                pass

    async def _spare_context(self, site: str) -> BrowserContext | None:
        """A second context of the website's browser, so two attempts do not share a sign-in or an access choice."""
        try:
            browser = self.contexts[site].browser
            return await browser.new_context(viewport={"width": 1280, "height": 800}) if browser else None
        except Exception as error:
            self.board.problem("specification", f"no second context ({type(error).__name__}); tabs share one")
            return None

    def _requirement_found(self, req: Requirement, site: str, url: str, seen: str, erp: Erp | None) -> None:
        """A requirement read by a specification attempt: on the board and to the ERP, once per id and only for
        the revision the requirements so far come from."""
        if self.spec_revision is None:
            self.spec_revision = req.revision
        if not same_revision(req.revision, self.spec_revision) or req.requirement_id in self.requirements:
            return
        self.requirements[req.requirement_id] = req
        self.board.post("requirement", req.requirement_id, req.raw or req.model_dump(mode="json"),
                        Source(site=site, url=url, quote=quote_for(seen, req.requirement_id, str(req.bore))),
                        "specification")
        if erp:
            erp.start(self.effective(req))

    async def _spec_attempt(self, site: str, tag: str, context: BrowserContext, brief: str, erp: Erp | None, *,
                            thinking: bool, steps: int) -> SpecResult:
        access = {**self.access[site], "siteId": f"{site}:{tag}"}
        await self.site_up(site)
        session = await start_session(SiteSession(access, context, self.trace), until=self.research_deadline)
        return await research_spec(
            session, self.briefing, context=brief, deadline=self.research_deadline, thinking=thinking,
            max_steps=steps,
            on_requirement=lambda req: self._requirement_found(req, site, session.page.url, session.seen, erp))

    def _adopt_revision(self, spec: SpecResult, site: str, erp: Erp | None) -> None:
        """The winning attempt's revision governs: requirements streamed from another revision are replaced."""
        if spec.current_revision and self.spec_revision and not same_revision(spec.current_revision, self.spec_revision):
            self.board.problem("specification", f"attempts disagree on the revision ({self.spec_revision} vs "
                                                f"{spec.current_revision}); the finished attempt's is used")
            for rid in list(self.requirements):
                task = erp.tasks.pop(rid, None) if erp else None  # researched for the other revision
                if task is not None and not task.done():
                    task.cancel()
            self.requirements = {}
            self.spec_revision = None
        for req in spec.requirements:
            self._requirement_found(req, site, "", "", erp)

    async def _post_materials(self, site: str) -> None:
        assert self.erp is not None
        for research in self.erp.requirements:
            for problem in research.problems:
                self.board.problem("materials", f"{research.requirement_id}: {problem}")
            record = self.erp.materials.get(research.selected) if research.selected else None
            value = {"selected": research.selected, "inspected": research.inspected,
                     "search_term": research.search_term}
            quote = ""
            if record is not None:
                value.update({k: (str(v) if isinstance(v, Decimal) else v) for k, v in record.model_dump(
                    mode="json", exclude={"raw", "conditions"}).items()})
                value["conditions"] = [c.model_dump(mode="json") for c in record.conditions]
                raw = record.raw or {}
                quote = f"status {raw.get('status_code', '')} {raw.get('status_meaning', '')}; stock {raw.get('free_stock', '')}"
            self.board.post("material", research.requirement_id, value, Source(site=site, quote=quote), "materials")

    async def mails(self, site: str) -> None:
        board = self.board
        try:
            # The tabs sign in while the planner reads the brief and waits for the tender's keys.
            # Staggered: several tabs signing in to one website at the same moment hung in local runs.
            starts = [asyncio.create_task(self._mail_tab(site, i)) for i in range(MAIL_TABS)]
            positions = (await board.wait("position", timeout=min(FINDINGS_WAIT, max(1.0, self.left())))
                         if FINDINGS_WAIT > 0 else board.get("position"))
            findings = "\n".join(f"- position {k}: " + ", ".join(
                f"{f} {v}" for f, v in fact.value.items() if f.endswith("_key") or f == "condition_group")
                for k, fact in positions.items())
            sites = {s["siteId"]: s["url"] for s in self.task["access"]}
            try:
                kinds = await self._plan_mail(sites, findings)
            except BaseException:
                for start in starts:  # no plan, no searches: the tabs would only compete for the browser
                    start.cancel()
                raise
            self.mail_kinds = [k for k in kinds if k.site == site]
            elsewhere = [f"{k.name} on {k.site}" for k in kinds if k.site != site]
            if elsewhere:  # another mail site would be its own job (the plan names every website it needs)
                board.problem("mails", f"planned on other websites, not searched here: {elsewhere}")
            self.trace.event("mails", "plan", kinds=[f"{k.name}: {k.subject} / {k.sender}" for k in self.mail_kinds])
            await self._research_mail(self.mail_kinds, starts=starts)
        except Exception as error:
            board.problem("mails", f"{type(error).__name__}: {str(error)[:200]}")
        finally:
            board.finish("mail")

    async def _plan_mail(self, sites: dict[str, str], findings: str) -> list:
        """
        The mail plan, with a patient second try: under platform load (model p50 11 s, p95 91 s in one soak run)
        every attempt of the short-timeout plan call timed out, and without a plan no mail kind was searched at all
        (discount, surcharges, packaging and bonus lost).
        """
        # (timeout per call, budget for all its retries): the quick try gets about two attempts, not seven
        tries = ((research_mail.PLAN_TIMEOUT, 2.5 * research_mail.PLAN_TIMEOUT), (PLAN_PATIENT_TIMEOUT, None))
        timeouts = [t for t, _ in tries]
        for i, (timeout, budget) in enumerate(tries):
            left = self.mail_deadline - time.monotonic()
            try:
                return await asyncio.wait_for(
                    research_mail.plan_searches(self.task["brief"], sites, findings, timeout=timeout),
                    max(1.0, min(left, budget or left)))
            except Exception as error:
                if i == len(timeouts) - 1 or self.mail_deadline - time.monotonic() < 2 * PLAN_PATIENT_TIMEOUT:
                    raise
                self.board.problem("mails", f"plan failed ({type(error).__name__}: {str(error)[:120]}); "
                                            f"planning again with {PLAN_PATIENT_TIMEOUT:g} s per call")
        raise AssertionError("unreachable")

    async def _mail_tab(self, site: str, i: int) -> SiteSession:
        await asyncio.sleep(MAIL_TAB_STAGGER * i)
        return await self.session(site, f"mail{i + 1}")

    def _kind_seconds(self, deadline: float, waiting: int) -> float:
        """One kind's search budget from the mail time left, shared with the kinds still waiting over the open
        tabs: more than the default while time is ample, never less than KIND_MIN_SECONDS."""
        tabs = max(1, len(self.mail_sessions))
        rounds = 1 + -(-waiting // tabs)  # this kind plus the waiting ones, a round per tab
        share = (deadline - 20 - time.monotonic()) / rounds
        return max(KIND_MIN_SECONDS, min(KIND_MAX_SECONDS, share))

    async def _mailbox(self) -> str:
        """The bidder's own address (its domain authorises the record senders): the first tab that shows it."""
        if not self.mailbox:
            for session in self.mail_sessions:
                self.mailbox = await research_mail.mailbox_address(session)
                if self.mailbox:
                    break
        return self.mailbox

    async def _research_mail(self, kinds: list, deadline: float | None = None, starts: list | None = None) -> None:
        """
        Search the record kinds over the mail tabs (one kind per tab at a time) until the mail deadline, then
        resolve whatever records were read: a kind still open at the deadline is dropped, the rest go to calc.
        Results accumulate, so a rework of some kinds keeps the others. `starts` are tabs still signing in; each
        joins as soon as it is ready (a tab that never opens just leaves the work to the others).
        """
        queue: asyncio.Queue = asyncio.Queue()
        # The approval goes first: the discount counts for points of its own and feeds every unit price. The
        # stroke reserves and the condition rules follow: materials (and so most positions) and the discount
        # scopes wait on them.
        first = ("approval", "end_reserves", "condition_rules")
        for kind in sorted(kinds, key=lambda k: first.index(k.name) if k.name in first else len(first)):
            queue.put_nowait((kind, 0))
        deadline = deadline or self.mail_deadline

        address: list[asyncio.Task] = []

        async def tab(source) -> None:
            if isinstance(source, SiteSession):
                session = source
            else:
                try:
                    session = await source
                except Exception:
                    return
                self.mail_sessions.append(session)
            if not address:  # the first open tab also looks up the mailbox address, alongside its searches
                address.append(asyncio.create_task(self._mailbox()))
            while not queue.empty() and time.monotonic() < deadline - 20:
                kind, tries = queue.get_nowait()
                try:
                    budget = self._kind_seconds(deadline, queue.qsize())
                    options = {"seconds": budget} if "seconds" in KIND_OPTIONS else {}
                    found = await research_mail.research_kind(session, kind, self.reference, deadline=deadline,
                                                               **options)
                except Exception as error:  # one failed kind must not cost the others
                    self.board.problem("mails", f"{kind.name}: {type(error).__name__}: {str(error)[:160]}")
                    continue
                earlier = self.mail_results.get(kind.name)
                if earlier is not None:  # a second try adds to what the first one read
                    found.records = [*earlier.records, *found.records]
                    found.kept = [*earlier.kept, *found.kept]
                self.mail_results[kind.name] = found
                stopped = getattr(found, "stopped", None)
                if stopped is None:  # out of its budget without reading a message
                    stopped = not found.records and found.seconds >= min(budget, getattr(research_mail, "KIND_SECONDS", budget)) - 5
                if (stopped and not tries
                        and deadline - time.monotonic() > MAIL_RETRY_SECONDS):
                    # Out of its budget: once more at the end of the queue, from a fresh start on this tab.
                    self.board.problem("mails", f"{kind.name}: search stopped at its budget; tried once more")
                    queue.put_nowait((kind, 1))

        tabs = [asyncio.create_task(tab(s)) for s in (starts or list(self.mail_sessions))]
        _, late = await asyncio.wait(tabs, timeout=max(1.0, deadline - time.monotonic()))
        for job in late:
            job.cancel()
        for start in starts or []:
            if not start.done():
                start.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)
        open_kinds = [k.name for k in kinds if k.name not in self.mail_results]
        if open_kinds:
            self.board.problem("mails", f"mail deadline: {open_kinds} not read; their fields stay blank")
        if not self.mail_sessions:
            raise ConnectionError("no mail tab could be opened")
        await self._resolve_mail(await address[0] if address else None)

    async def _resolve_mail(self, mailbox: str | None = None) -> None:
        """The records read so far, all kinds, resolved into mail facts and posted on the board."""
        mailbox = mailbox if mailbox is not None else await self._mailbox()
        facts = research_mail.resolve_mail(list(self.mail_results.values()), self.reference,
                                           mailbox.split("@", 1)[1] if "@" in mailbox else "", self.scenario, mailbox)
        if not mailbox:
            facts.problems.append("mailbox address not found: no sender could be authorised")
        for problem in facts.problems:
            self.board.problem("mails", problem)
        self.mail_facts = facts
        site = self.mail_sessions[0].site["siteId"].split(":")[0]
        used = {}
        for s in getattr(facts, "sources", []):
            if s.used:
                used.setdefault(s.kind, []).append(f"{s.subject} | {s.sender} | {s.sent_at.isoformat()} | {s.reference}")
        def post(kind: str, key: str, value) -> None:
            self.board.post("mail", f"{kind}/{key}", value if isinstance(value, (str, int, float, dict, list))
                            else value.model_dump(mode="json") if hasattr(value, "model_dump") else str(value),
                            Source(site=site, quote="; ".join(used.get(kind, [])) or "(no record used)"), "mails")
        if facts.approval is not None:
            post("approval", "latest", facts.approval)
        for table, kind in (("discount_scopes", "condition_rules"), ("metal_indices", "metal_indices"),
                            ("end_reserves", "end_reserves"), ("packaging", "packaging"),
                            ("commitments", "commitments"), ("fx_rates", "fx"), ("fx_per_eur", "fx"),
                            ("setup_pools", "setup_pools"),
                            ("project_bonuses", "project_bonus")):
            for key, value in (getattr(facts, table, None) or {}).items():
                if table == "fx_per_eur":  # quoted the other way round; calc divides by it
                    value = {"currency_units_per_eur": str(value)}
                post(kind, key, [v.model_dump(mode="json") for v in value] if isinstance(value, list) else value)

    # ------------------------------------------------------------ the pipeline

    async def research(self) -> None:
        self.plan = await plan_research(self.task, self.briefing)
        self.trace.event("plan", "done", needs=[f"{n.kind}@{n.site}: {n.why}" for n in self.plan.needs])
        sites: dict[str, list[str]] = {}  # a role may be spread over several websites in another dataset
        for n in self.plan.needs:
            if n.site not in sites.setdefault(n.kind, []):
                sites[n.kind].append(n.site)
        jobs = []
        if "tender" in sites and research_tender is not None:
            for site in sites["tender"]:
                first = asyncio.create_task(self.tender(site), name=f"tender@{site}")
                jobs += [first, asyncio.create_task(self._tender_watch(site, first), name=f"tender-watch@{site}")]
        elif "tender" in sites:
            self.board.problem("plan", "tender research module not available")
        erp_sites = sites.get("materials", [])
        if len(erp_sites) > 1:
            self.board.problem("plan", f"materials on several sites {erp_sites}; only the first is searched")
        for site in sites.get("specification", []):
            jobs.append(asyncio.create_task(self.specification(site, erp_sites[0] if erp_sites else None),
                                            name=f"specification@{site}"))
        if "mails" in sites and research_mail is not None:
            jobs += [asyncio.create_task(self.mails(site), name=f"mails@{site}") for site in sites["mails"]]
        elif "mails" in sites:
            self.board.problem("plan", "mail research module not available")
        if not jobs:
            return
        done, pending = await asyncio.wait(jobs, timeout=max(1.0, self.left()))
        for job in pending:  # the deadline guard: whatever is complete goes on
            job.cancel()
            self.board.problem("deadline", f"{job.get_name()} stopped at the research deadline")
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._erp is not None:  # ERP workers still browsing would compete with the bid form for the browser
            for task in [*self._erp.tasks.values(), *self._erp.materials.values()]:
                if not task.done():
                    task.cancel()
            if self.erp is None:  # stopped at the deadline: what finished still counts
                so_far = getattr(self._erp, "so_far", None)
                self.erp = so_far() if so_far is not None else self._finished_erp()
        self.rework = {"mails": self._rework_mail}

    def _finished_erp(self) -> ErpResearch:
        def finished(tasks):
            return {k: t.result() for k, t in tasks.items()
                    if t.done() and not t.cancelled() and t.exception() is None and t.result() is not None}
        return ErpResearch(requirements=list(finished(self._erp.tasks).values()),
                           materials=finished(self._erp.materials))

    async def _rework_mail(self, doubtful: list) -> None:
        names = {f.key.split("/")[0] for f in doubtful}
        kinds = [k for k in self.mail_kinds if k.name in names]
        if kinds and self.mail_sessions:
            await self._research_mail(kinds, min(self.research_deadline, time.monotonic() + REWORK_SECONDS))

    async def verify(self) -> None:
        doubtful = await verify_board(self.board, self.rules, self.reference, self.briefing.scenario_date)
        by_worker: dict[str, list] = {}
        for fact in doubtful:
            by_worker.setdefault(fact.worker, []).append(fact)
        if GAP_REWORK:  # the doubtful mail kinds are searched again in the gap pass, alongside the other retries
            return
        for worker, facts in by_worker.items():
            rework = self.rework.get(worker)
            if rework is None or self.left() < REWORK_SECONDS:
                continue
            self.trace.event("verify", "rework", by=worker, facts=[f.ident for f in facts])
            try:
                await asyncio.wait_for(rework(facts), max(1.0, self.left()))
            except Exception as error:
                self.board.problem("verify", f"rework {worker}: {type(error).__name__}: {str(error)[:160]}")
        if by_worker:
            await verify_board(self.board, self.rules, self.reference, self.briefing.scenario_date)

    async def select_materials(self) -> None:
        """The final material choice, once amendments and reserves are on the board (reselect: no browsing)."""
        if self.erp is None or not self.requirements:
            return
        effective = [self.effective(req) for req in self.requirements.values()]
        own = {r.requirement_id: r.selected for r in self.erp.requirements}
        # One choice per requirement, side by side: one hung model call must not cost the others; choices not made
        # in time are the ERP workers' own.
        tasks = {req.requirement_id: asyncio.create_task(reselect([req], self.erp, self.trace)) for req in effective}
        _, late_tasks = await asyncio.wait(tasks.values(), timeout=SELECT_SECONDS)
        for task in late_tasks:
            task.cancel()
        if late_tasks:
            await asyncio.gather(*late_tasks, return_exceptions=True)
        chosen = {}
        for rid, task in tasks.items():
            if task.done() and not task.cancelled() and task.exception() is None:
                chosen.update(task.result())
        late = sorted(set(own) - set(chosen))
        if late:
            self.board.problem("select", f"no new choice in time for {late}; the ERP workers' choices are used")
        self.chosen = {**own, **chosen}
        self.trace.event("select", "done", chosen=self.chosen)

    def _unknown_clauses(self, p, clause, pools, bonuses) -> None:
        """A position whose Q&A names a key the mails did not resolve: calc would report a zero for that part,
        which reads as a known zero, so the report field and the unit price it feeds are left blank instead."""
        fields = set()
        if getattr(p, "packaging_key", None) and getattr(clause, "packaging", None) is None:
            fields |= {"packaging_per_unit", "unit_price"}
        if getattr(p, "setup_pool_key", None) and not any(p.position in x.members for x in pools or []):
            fields |= {"surcharge_per_unit", "unit_price"}
        if getattr(p, "bonus_key", None) and not any(p.position in x.members for x in bonuses or []):
            fields |= {"rebate_total", "unit_price"}
        if fields:
            self.blank_hints.setdefault(p.position, set()).update(fields)

    def _estimate(self, req_id: str, research) -> str | None:
        """
        A requirement whose stroke still waits on a per-end reserve (rule 20) at calc time: the best estimate among
        the inspected active materials that equal it in every other attribute is the one with the smallest stroke
        that still exceeds the usable travel (nominal = usable + 2 x reserve, so never less). An estimate keeps
        the position's quantity and other points that an omitted position loses; the gap pass still treats the
        requirement as open, so the reserve is searched for while time is left.
        """
        if research is None or self.erp is None:
            return None
        req = self.requirements[req_id]
        effective = self.effective(req)
        usable = Decimal(str(calc.apply_amendments({"stroke": req.stroke}, self._amendments(), req.revision,
                                                   req.requirement_id, self.scenario_stamp)["stroke"]))
        best: tuple[Decimal, str] | None = None
        for number in research.inspected:
            record = self.erp.materials.get(number)
            if record is None or not record.active:
                continue
            attrs = merged_attributes(record, research)
            stroke = attrs.get("stroke")
            if mismatches(effective, attrs) or not isinstance(stroke, Decimal) or stroke <= usable:
                continue
            if best is None or stroke < best[0]:
                best = (stroke, number)
        return best[1] if best else None

    def build_bid(self) -> tuple[calc.BidInput, list[str]]:
        self.blank_hints = {}
        self.attributes = {}
        problems: list[str] = []
        clauses = None
        if self.tender_facts is not None and self.mail_facts is not None and research_mail is not None:
            try:
                clauses = research_mail.bid_clauses(self.tender_facts, self.mail_facts)
                problems += list(clauses.problems)
            except Exception as error:
                problems.append(f"bid clauses: {type(error).__name__}: {str(error)[:160]}")
        by_position = {c.position: c for c in getattr(clauses, "positions", [])}
        positions = []
        for p in getattr(self.tender_facts, "positions", []) or []:
            req_id = next((rid for rid in self.requirements if _same_id(rid, p.requirement)), None)
            if req_id is None:
                problems.append(f"position {p.position}: requirement {p.requirement} not read; position omitted")
                continue
            research = next((r for r in (self.erp.requirements if self.erp else []) if r.requirement_id == req_id),
                            None)
            # without a final choice (reselect cut short) the ERP worker's own choice stands
            number = self.chosen[req_id] if req_id in self.chosen else getattr(research, "selected", None)
            # a catalogue group the site itself reported empty is searched with no match (rule 3), not a gap
            empty_group = bool(getattr(research, "no_hits", False)) and not getattr(research, "inspected", None)
            if number is None and not empty_group and (research is None or not research.inspected):
                problems.append(f"position {p.position}: ERP not researched; position omitted")
                continue
            if number is None and not empty_group:
                # No material only means no_bid (rule 3) when the search for it was complete; a requirement still
                # pending (e.g. a reserve not found) or a search that hit problems leaves the position out instead.
                pending = self.effective(self.requirements[req_id]).pending
                estimate = self._estimate(req_id, research) if pending else None
                if estimate:
                    # Only here: no material was chosen (never replaces a choice) and the reserve is still unknown.
                    # The position was omitted before, not a no_bid, so the decision does not change either.
                    self.trace.event("calc", "stroke_estimate", position=p.position, requirement=req_id,
                                     material=estimate, pending="; ".join(pending)[:160])
                    problems.append(f"position {p.position}: {'; '.join(pending)[:120]}; material {estimate} "
                                    f"estimated (the closest stroke above the usable travel)")
                    number = estimate
                elif pending or (research is not None and research.problems):
                    problems.append(f"position {p.position}: material undecided "
                                    f"({'; '.join(pending or research.problems)[:160]}); position omitted")
                    continue
            material = self.erp.materials.get(number) if (number and self.erp) else None
            if number and material is None:  # chosen, but its record was not read: a gap, never "no material"
                problems.append(f"position {p.position}: record of {number} not read; position omitted")
                continue
            if material is not None:
                try:  # after amendments and the usable-travel derivation
                    wanted = self.effective(self.requirements[req_id])
                except Exception:
                    wanted = None
                self.attributes[p.position] = material_attributes(material, wanted)
            clause = by_position.get(p.position)
            self._unknown_clauses(p, clause, getattr(clauses, "setup_pools", None),
                                  getattr(clauses, "project_bonuses", None))
            extra = ({"discount_scope": clause.discount_scope, "setup_fee": clause.setup_fee,
                      "packaging": clause.packaging, "commitment": clause.commitment} if clause
                     else {"setup_fee": getattr(p, "setup_fee", Decimal(0))})
            pieces_per_set = self.effective(self.requirements[req_id]).pieces_per_set  # amended set size (rule 14)
            try:
                positions.append(to_position_input(p.position, p.quantity, p.unit, self.requirements[req_id],
                                                   material, pieces_per_set=pieces_per_set, **extra))
            except calc.CalcError as error:
                if material is not None:  # a reading gap (e.g. the unit): this position out, never "no material"
                    problems.append(f"position {p.position}: {error}; position omitted")
                    continue
                # no matching material, established: it carries the no_bid decision whatever its unit reads
                problems.append(f"position {p.position}: {error}")
                positions.append(calc.PositionInput(position=p.position, quantity=p.quantity, material=None,
                                                    **{k: v for k, v in extra.items() if v is not None}))
        bid = calc.BidInput(tender_reference=self.reference, scenario_date=self.scenario_stamp, positions=positions,
                            approval=getattr(clauses, "approval", None) or getattr(self.mail_facts, "approval", None),
                            fx_rates=getattr(clauses, "fx_rates", None) or getattr(self.mail_facts, "fx_rates", {}) or {},
                            metal_indices=getattr(clauses, "metal_indices", None)
                            or getattr(self.mail_facts, "metal_indices", {}) or {},
                            fx_per_eur=getattr(clauses, "fx_per_eur", None) or {},
                            setup_pools=getattr(clauses, "setup_pools", None) or [],
                            project_bonuses=getattr(clauses, "project_bonuses", None) or [])
        return bid, problems

    def calculate(self) -> Calculation:
        """_calculate() with the form attributes it set, for the gap pass (run again after each gap round)."""
        result, blank, problems = self._calculate()
        return Calculation(result, blank, problems, dict(self.attributes))

    def fallback_answer(self) -> dict:
        """
        A bid with every position omitted: the reference, the decision, the discount of an approval that was read
        (a conditional one only when no positions exist to meet its threshold means 0, rule 16) and total 0.
        """
        try:
            approval = getattr(self.mail_facts, "approval", None)
            bid = calc.BidInput(tender_reference=self.reference, scenario_date=self.scenario_stamp, positions=[],
                                approval=approval if isinstance(approval, calc.Approval) else None)
            return calc.calculate(bid).answer()
        except Exception:
            return {"tender_reference": self.reference, "decision": "bid", "currency": "EUR",
                    "discount_percent": 0.0, "positions": [], "total_net": 0.0}

    def _calculate(self) -> tuple[calc.BidResult | None, dict[str, set[str]], list[str]]:
        bid, problems = self.build_bid()
        self.trace.event("calc", "input", positions=[p.position for p in bid.positions], problems=problems)
        if not bid.positions:
            self.board.problem("calc", "no position could be built; the bid goes in with every position omitted")
        result, blank, calc_problems = safe_calculate(bid)
        for position, fields in self.blank_hints.items():
            blank.setdefault(position, set()).update(fields)
        return result, blank, problems + calc_problems

    def _tender_sites(self) -> list[str]:
        return list(dict.fromkeys(n.site for n in getattr(self.plan, "needs", []) if n.kind == "tender"))

    async def recheck_qa(self, hint: str, until: float) -> list[str]:
        """
        One more read of the bidder Q&A, on a fresh tab of each tender website, told what to look for; answers the
        earlier reads lack are merged in (research_tender.combine). Returns the amendments that are new.
        """
        if research_tender is None or not hasattr(research_tender, "research_qa"):
            return []
        before = {research_tender._amendment_text(a) for a in self._amendments()}

        async def one(site: str) -> None:
            self.tender_calls += 1
            tag = f"recheck{self.tender_calls}"
            session = await self.session(site, tag)
            try:
                facts = await research_tender.research_qa(session, self.reference, self.scenario, deadline=until,
                                                          hint=hint)
                self._tender_read(f"{site}#{self.tender_calls}{tag}", facts, site, session)
            finally:
                for page in list(getattr(session, "pages", [])):
                    try:
                        await page.close()
                    except Exception:  # already gone
                        continue

        results = await asyncio.gather(*(one(site) for site in self._tender_sites()), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                self.board.problem("tender", f"Q&A re-check: {type(result).__name__}: {str(result)[:160]}")
        return [text for text in (research_tender._amendment_text(a) for a in self._amendments())
                if text not in before]

    def _mismatch_focus(self, missing: list[str]) -> tuple[list[str], str]:
        """The requirements of the positions without material and the attributes their inspected materials miss
        them by, as a hint for the Q&A re-check."""
        positions = {p.position: p for p in getattr(self.tender_facts, "positions", []) or []}
        rids, parts = [], []
        for position in missing:
            p = positions.get(position)
            rid = next((r for r in self.requirements if _same_id(r, p.requirement)), None) if p else None
            if rid is None or rid in rids:
                continue
            rids.append(rid)
            research = next((r for r in getattr(self.erp, "requirements", []) or [] if r.requirement_id == rid), None)
            keys: list[str] = []
            for number in getattr(research, "inspected", []) or []:
                record = (getattr(self.erp, "materials", {}) or {}).get(number)
                if record is None or not record.active:
                    continue
                for diff in mismatches(self.effective(self.requirements[rid]), merged_attributes(record, research)):
                    key = diff.split()[0]
                    if key not in keys and not diff.endswith("not shown"):
                        keys.append(key)
            parts.append(f"{rid}" + (f" ({', '.join(keys)})" if keys else ""))
        hint = ("Read every answer, on every page of the list. Look in particular for technical amendments "
                f"(changed attribute values) of requirement {'; '.join(parts)} of the specification.") if parts else ""
        return rids, hint

    async def recheck_no_bid(self, missing: list[str]):
        """
        Before a no_bid for positions without a matching material stands: one targeted re-read of the tender Q&A
        for amendments to the mismatching requirements (in one sandbox run the Q&A paging stopped at a page that
        had not rendered and an amendment on it was missed, so the unamended stroke rejected the matching
        material). New amendments lead to a new choice for those requirements and a new calculation, which is
        returned; None when nothing new was found (the no_bid stands on complete evidence).
        """
        rids, hint = self._mismatch_focus(missing)
        budget = min(RECHECK_SECONDS, self.form_left() - 40)
        if not rids or not hint or budget < 45 or not self._tender_sites():
            return None
        started = time.monotonic()
        self.trace.event("verify", "no_bid_recheck", requirements=rids, seconds_left=round(budget))
        try:
            new = await asyncio.wait_for(self.recheck_qa(hint, started + budget - 5), budget)
        except Exception as error:
            self.board.problem("verify", f"Q&A re-check: {type(error).__name__}: {str(error)[:160]}")
            return None
        self.trace.event("verify", "no_bid_recheck_done", new_amendments=new,
                         seconds=round(time.monotonic() - started, 1))
        if not new:
            return None
        tasks = {rid: asyncio.create_task(reselect([self.effective(self.requirements[rid])], self.erp, self.trace))
                 for rid in rids}
        _, late = await asyncio.wait(tasks.values(), timeout=max(10.0, min(SELECT_SECONDS, self.form_left() - 30)))
        for task in late:
            task.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)
        for task in tasks.values():
            if task.done() and not task.cancelled() and task.exception() is None:
                self.chosen.update(task.result())
        self.trace.event("select", "recheck", chosen={rid: self.chosen.get(rid) for rid in rids})
        return self._calculate()

    async def wording_matches(self, missing: list[str]) -> dict[str, str]:
        """
        For the positions calc found without material: an active inspected material whose numbers all equal the
        requirement and whose text attributes differ, checked by one thinking call for whether that is only
        wording. Returns requirement -> material only when it covers every missing position with exactly one
        material each (otherwise the no_bid stands, and nothing is asked when no such material exists).
        """
        if self.erp is None:
            return {}
        positions = {p.position: p for p in getattr(self.tender_facts, "positions", []) or []}
        cases: dict[str, str] = {}
        by_case: dict[str, tuple[str, str]] = {}
        needed: set[str] = set()
        for position in missing:
            p = positions.get(position)
            req_id = next((rid for rid in self.requirements if _same_id(rid, p.requirement)), None) if p else None
            research = next((r for r in self.erp.requirements if r.requirement_id == req_id), None)
            if req_id is None or research is None:
                return {}
            needed.add(req_id)
            want = self.effective(self.requirements[req_id])
            for number in research.inspected:
                record = self.erp.materials.get(number)
                if record is None or not record.active:
                    continue
                keys = wording_differences(want, merged_attributes(record, research))
                if not keys:
                    continue
                shown = next((c for c in research.candidates if c.material_number == number), None)
                case = f"{req_id}/{number}"
                cases[case] = "; ".join(
                    f"{key}: requirement {getattr(want, key)!r}, material "
                    f"{(getattr(record, key) or getattr(shown, key, ''))!r}" for key in keys)
                by_case[case] = (req_id, number)
        if not cases or {rid for rid, _ in by_case.values()} != needed:
            return {}
        same = await wording_check(cases)
        self.trace.event("verify", "wording", cases=cases, same=sorted(same or ()))
        picks: dict[str, set[str]] = {}
        for case in same or ():
            req_id, number = by_case[case]
            picks.setdefault(req_id, set()).add(number)
        if set(picks) != needed or any(len(numbers) != 1 for numbers in picks.values()):
            return {}
        return {req_id: numbers.pop() for req_id, numbers in picks.items()}

    async def run(self) -> tuple[dict | None, calc.BidResult | None]:
        """Research, verify, calculate: the answer dict for the bid form. Positions that are not complete are
        omitted; with none complete it is still a bid with reference, decision and discount."""
        await self.research()
        # The deadline guard after research: each step runs only in the time the bid form leaves it.
        # The verifier runs beside the rest: its doubts alone never change the answer (they only add a doubted mail
        # kind to a gap round that has a real gap and a spare tab), and awaiting its one long call held the answer
        # back 18 to 47 s in every measured run. The gap pass waits for it only when a round with real gaps runs.
        self.verifying = (asyncio.create_task(self._bounded(self.verify(), "verify"))
                          if self.form_left() > 60 and VERIFY_ALONGSIDE else None)
        if self.verifying is None and self.form_left() > 60:
            await self._bounded(self.verify(), "verify")
        try:
            return await self._answer()
        finally:
            if self.verifying is not None and not self.verifying.done():
                self.verifying.cancel()
                await asyncio.gather(self.verifying, return_exceptions=True)

    async def _answer(self) -> tuple[dict | None, calc.BidResult | None]:
        await self._bounded(self.select_materials(), "select")
        # The second pass: what the first answer lacks is retried while research time is left (gaps.py).
        best = await GapFiller(self).run(self.calculate(), self.calculate)
        result, blank, problems, self.attributes = best.result, best.blank, best.problems, best.attributes
        if result is not None and result.decision == "no_bid" and result.missing_materials and self.form_left() > 90:
            # A no_bid stands only on complete evidence: the Q&A is read once more for amendments to the
            # requirements that found no material.
            again = await self.recheck_no_bid(result.missing_materials)
            if again is not None and again[0] is not None:
                if again[0].decision != result.decision:
                    self.trace.event("verify", "no_bid_overturned", by="Q&A re-check")
                result, blank, problems = again
        if result is not None and result.decision == "no_bid" and result.missing_materials and self.form_left() > 45:
            # Before a no_bid is accepted: are the mismatches that leave positions without material only wording?
            try:
                rescued = await asyncio.wait_for(self.wording_matches(result.missing_materials),
                                                 max(1.0, min(VERIFY_SECONDS, self.form_left() - 30)))
            except Exception as error:
                rescued = {}
                self.board.problem("verify", f"wording check: {type(error).__name__}: {str(error)[:160]}")
            if rescued:
                self.chosen = {**self.chosen, **rescued}
                again = self._calculate()
                if again[0] is not None and again[0].decision == "bid":
                    self.trace.event("verify", "no_bid_overturned", chosen=rescued)
                    result, blank, problems = again
        if result is None:  # still a bid: reference, decision and discount score without positions
            answer = self.fallback_answer()
            self.trace.event("calc", "answer", answer=answer, problems=problems, fallback=True)
            return answer, None
        # The check can only turn a bid with missing materials into no_bid; calc never returns such a bid today,
        # so it is asked only then and costs the bid form no time otherwise.
        disputable = result.decision == "bid" and bool(result.missing_materials)
        check = None if self.form_left() < 30 or not disputable else await confirm_decision(
            result.decision, self.board.summary() + "\n\nProblems:\n" + "\n".join(self.board.problems + problems),
            self.rules)
        if check is not None and not check.agrees:
            self.trace.event("verify", "decision_disputed", proposed=result.decision, wanted=check.decision,
                             reason=check.reason)
        if check is not None and check.decision == "no_bid" and result.decision == "bid" and result.missing_materials:
            result = result.model_copy(update={"decision": "no_bid", "positions": [], "total_net": Decimal("0.00")})
        answer = answer_from(result, blank)
        self.trace.event("calc", "answer", answer=answer, problems=problems, blank={k: sorted(v) for k, v in blank.items()})
        return answer, result
