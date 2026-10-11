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
from research import MATERIAL_SECONDS, Erp, ErpResearch, SpecResult, research_spec, reselect, same_revision
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
# v3: 360 (was 300). submit.py stops entering positions SIGNING_RESERVE (180) before the end, so a form that
# starts at the last moment has 180 s for its steps and positions (measured: 193 s, and 352 s on a slow bid site).
FORM_SECONDS = float(os.environ.get("AGENT_FORM_SECONDS", "360"))  # the bid form and signing need this much
# Mail is the longest chain of the research: nine record kinds of 40 to 190 s each took three rounds over 4 tabs
# and two over 6 (three kinds waited 50 to 90 s for a free tab, and the research ended when they did). With a tab
# per kind none waits. Each tab is one more sign-in on the webmail; MAIL_TABS lowers it again.
MAIL_SURVEY = os.environ.get("AGENT_MAIL_SURVEY", "1") != "0"  # read the mailbox once for all kinds first
# With the survey one tab reads the mailbox for every kind (messages open in short-lived tabs of their own), and
# the others only search what it did not find: four are enough, and each tab not opened is a sign-in the webmail
# is spared (in one run five of nine failed to open).
MAIL_TABS = int(os.environ.get("MAIL_TABS", "") or (4 if MAIL_SURVEY else 9))
SPEC_STEPS_FAST = int(os.environ.get("SPEC_STEPS_FAST", "25"))  # navigation steps of the fast spec attempt
SPEC_STEPS_THINKING = int(os.environ.get("SPEC_STEPS_THINKING", "35"))  # and of the careful one, run alongside
SELECT_SECONDS = 75.0  # the final material choice (a model call per requirement, in parallel)
KIND_OPTIONS = set(inspect.signature(research_mail.research_kind).parameters) if research_mail else set()
KIND_MIN_SECONDS = 120.0  # a mail record kind's search budget: at least this
KIND_MAX_SECONDS = 240.0  # and at most this, from the mail time left per kind
MAIL_RETRY_SECONDS = 90.0  # mail time a kind's second try needs
# A kind the survey listed every folder for (reference and topic) without a single row: one more search in other
# words, short and without a second try. In an eval a record that did not exist was searched for 215 s.
ABSENT_SECONDS = 60.0
MAIL_TAB_STAGGER = 4.0  # seconds between the mail tabs' sign-ins
# A mail search that takes much longer than the others took sits on a tab that hangs (one run: eight kinds done
# within 118 s, the ninth held the research until 275 s). A tab with nothing left to do then searches that kind
# too, and the first result ends both. The measure is this run's own searches, not a number from another run.
STRAGGLER = 1.5  # times the median of the finished searches
STRAGGLER_AFTER = 3  # finished searches needed before one is called slow
# Coverage is one more check on the research, and what fails it goes back to its worker instead of being left
# out: a requirement whose search listed nothing (and was not told "no hits" by the website) or whose materials
# were not all read is researched again, this often at most and only with time for a search and a material.
COVERAGE_ROUNDS = 3
COVERAGE_SECONDS = MATERIAL_SECONDS + 30
MAIL_SECONDS = float(os.environ.get("AGENT_MAIL_SECONDS", "600"))  # from the orchestrator's start (v3: was 540)

# v3: the specification is found by several attempts at once, each with its own strategy, in its own context of
# the website's browser (so they share neither a sign-in nor an access choice). The first that returns a revision
# with requirements wins and the others stop; a hard cutoff from the orchestrator's start leaves the ERP and the
# bid form their time.
SPEC_RACE_SECONDS = float(os.environ.get("AGENT_SPEC_RACE_SECONDS", "330"))
SPEC_GRACE = 5.0  # seconds after the cutoff an attempt may still take to return what it has
SPEC_STAGGER = 3.0  # seconds between the attempts' sign-ins (simultaneous sign-ins to one site hung locally)
SPEC_VARIANTS = [v.strip() for v in os.environ.get("AGENT_SPEC_VARIANTS", "fast,search").split(",")
                 if v.strip()]
SPEC_STRATEGIES = {
    "fast": {"thinking": False, "strong": False, "steps": SPEC_STEPS_FAST, "hint": ""},
    "thinking": {"thinking": True, "strong": False, "steps": SPEC_STEPS_THINKING, "hint": ""},
    "search": {"thinking": True, "strong": False, "steps": SPEC_STEPS_THINKING, "hint": (
        "Strategy for this attempt: start with the website's own search or document filter. Search for the tender "
        "reference, then for words of the tender's subject, and open the matching document from the hits. Use "
        "menus and long lists only if searching finds nothing.")},
    "explore": {"thinking": True, "strong": True, "steps": SPEC_STEPS_THINKING, "hint": (
        "Strategy for this attempt: first get an overview. Look at every menu, tab and section the start page "
        "offers and decide where documents and their versions could be before you open lists. Once the document "
        "is open, its revision history may sit behind a tab, a versions, history, changes or revisions link, a "
        "details or info button, a tooltip, an expandable section or an embedded viewer: check each of those "
        "before paging. Never page back and forth through the same list.")},
}
# How long the mail planner waits for the tender's keys before planning from the brief alone. 0: it plans at
# once, with whatever positions are on the board. (With 45 s the wait timed out in three of three runs and the
# mail searches started 75 to 170 s into the research, with the tabs signed in since about 25 s.)
FINDINGS_WAIT = float(os.environ.get("AGENT_FINDINGS_WAIT", "0"))
PLAN_PATIENT_TIMEOUT = 90.0  # per call when the quick mail plan failed: a loaded model answers in up to ~90 s
REWORK_SECONDS = 150.0  # a doubtful fact goes back to its worker only with at least this much research time left
# Off unless AGENT_REWORK=1: in seven sandbox runs a doubt sent every mail kind back to its search (120 to 170 s,
# plus a second check) and never changed a fact: the doubts were about the quote on the board, which is only the
# message's header, and the model's own label for that (record or evidence) differed between two checks of the
# same facts. The mail rules themselves (exact reference, sender, date) are applied in code either way. Without
# rework the verifier judges for the run log, beside the steps that follow, and costs the bid no time.
REWORK = os.environ.get("AGENT_REWORK", "0") == "1"
VERIFY_LOG = os.environ.get("AGENT_VERIFY", "0") == "1"  # judge the facts for the run log only (one large call)


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
    needs: list[Need] = Field(description="Every kind of fact the bid needs, each with its website; leave out "
                                          "what the rules do not require")


async def plan_research(task: dict, briefing: Briefing) -> Plan:
    """One model call: from the brief, which facts the bid needs and which website each comes from."""
    ids = {s["siteId"] for s in task["access"]}
    sites = "\n".join(f"- siteId {s['siteId']}: {s['url']}" for s in task["access"])
    kinds = "\n".join(f"- {k}: {v}" for k, v in FACT_KINDS.items())
    agent = Agent(llm.model(), output_type=Plan, model_settings=llm.settings(llm.thinking("plan", False)), retries=3,
                  instructions="You plan the research for a tender bid from its brief: which kinds of facts the "
                               "bid rules need, and on which of the run's websites each kind is found. Use the "
                               "siteIds exactly.")

    @agent.output_validator
    def _known(plan: Plan) -> Plan:
        bad = sorted({n.site for n in plan.needs} - ids)
        if bad:
            raise ModelRetry(f"{bad} are not siteIds of this run: {sorted(ids)}")
        return plan

    brief = task["brief"]
    prompt = (f"Websites of this run:\n{sites}\n\nKinds of facts the workers can establish:\n{kinds}\n\n"
              f"The bid is entered on {briefing.bid_site}; the bidder's mailbox is {briefing.mail_site}.\n\n"
              f"Brief:\n{brief}")
    return (await agent.run(prompt)).output


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


def unread_attributes(research, materials: dict) -> list[str]:
    """
    The inspected materials of a requirement that were never fully compared: a number (bore, stroke, pressure) or
    the mounting that neither the hit list nor the material's record showed. "No material matches" is established
    only when every candidate was compared in every attribute; an unread one leaves the position out instead of
    turning the whole bid into no bid. (Certification is not counted: a material without one shows nothing.)
    """
    out = []
    for number in getattr(research, "inspected", None) or []:
        record = materials.get(number)
        cand = next((c for c in getattr(research, "candidates", []) if c.material_number == number), None)
        if record is None:
            out.append(f"{number}: record not read")
            continue
        missing = [key for key in ("bore", "stroke", "pressure")
                   if getattr(record, key, None) is None and getattr(cand, key, None) is None]
        if not getattr(record, "mounting", "") and not getattr(cand, "mounting", ""):
            missing.append("mounting")
        if not getattr(record, "active", True) and "status meaning not read" in (getattr(record, "problems", None) or []):
            missing.append("status meaning")  # called inactive without a legend: a guess, not a blocked material
        if missing:
            out.append(f"{number}: {', '.join(missing)} not read")
    return out


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
            if bid.setup_pool is not None:
                bid = bid.model_copy(update={"setup_pool": None})
                problems.append(f"setup pool dropped: {message}")
            elif bid.project_bonus is not None:
                bid = bid.model_copy(update={"project_bonus": None})
                problems.append(f"project bonus dropped: {message}")
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


def material_attributes(material) -> dict[str, str]:
    """The chosen material's attributes as the ERP shows them (the bid form's cascading choices lead to it)."""
    out = {}
    for key in ("bore", "stroke", "pressure", "mounting", "certification"):
        value = getattr(material, key, None)
        if value not in (None, ""):
            out[key] = format(value, "f") if isinstance(value, Decimal) else str(value)
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
        self.started = time.monotonic()
        self.research_deadline = deadline - SUBMIT_SECONDS
        self.board = Board(trace)
        self.reference = briefing.tender_reference
        try:
            self.scenario = scenario_time(briefing.scenario_date)
        except Exception:
            # A brief that names no scenario date means today. Without this the whole research ended here, with
            # no position, on the first world whose brief had no date (an eval's starter world, 2.3 s in).
            self.scenario = datetime.now(UTC)
            self.board.problem("briefing", f"no scenario date in the brief ({briefing.scenario_date!r}); "
                                           "today is used")
        self.scenario_day: date = self.scenario.date()
        # calc compares two timestamps as instants and anything date-only by day: hand it the brief's own grain.
        self.scenario_stamp: date | datetime = (self.scenario if re.search(r"\d:\d\d", briefing.scenario_date)
                                                else self.scenario_day)
        self.rules = rules_text(task["brief"])
        self.plan: Plan | None = None
        self.tender_facts = None
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

    async def tender(self, site: str) -> None:
        board = self.board
        try:
            session, qa_session = await asyncio.gather(self.session(site, "tender"), self.session(site, "qa"),
                                                       return_exceptions=True)
            if isinstance(session, BaseException):
                if isinstance(qa_session, BaseException):
                    raise session
                session, qa_session = qa_session, None
            elif isinstance(qa_session, BaseException):
                board.problem("tender", f"no second tab ({type(qa_session).__name__}); one worker reads all")
                qa_session = None

            def on_facts(facts) -> None:
                self.tender_facts = facts
                self._post_tender(facts, site, session)

            facts = await research_tender.research_tender(session, self.reference, self.scenario,
                                                          deadline=self.research_deadline, qa_session=qa_session,
                                                          on_facts=on_facts)
            if facts is None:
                board.problem("tender", "no positions found")
                return
            if self.tender_facts is not facts:  # in case the module did not report it
                on_facts(facts)
        except asyncio.CancelledError:
            if self.tender_facts is not None:
                board.problem("tender", "stopped at the deadline: positions without the full Q&A are used")
            raise
        except Exception as error:
            board.problem("tender", f"{type(error).__name__}: {str(error)[:200]}")
        finally:
            for kind in ("position", "amendment", "withdrawn"):
                board.finish(kind)

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
        """Per-end reserves by key, only from records for the requirement's own spec revision (they are
        revision-specific); a record that names no revision counts for any."""
        reserves = getattr(self.mail_facts, "end_reserves", None) or {}
        return {key: Decimal(str(getattr(entry, "reserve_per_end_mm", entry))) for key, entry in reserves.items()
                if not getattr(entry, "revision", "") or _same_id(getattr(entry, "revision", ""), revision)
                or same_revision(getattr(entry, "revision", ""), revision)}

    def effective(self, req: Requirement) -> EffectiveRequirement:
        return effective_requirement(req, amendments=self._amendments(), reserves=self._reserves(req.revision),
                                     scenario_date=self.scenario_stamp)

    async def specification(self, site: str, erp_site: str | None) -> None:
        """
        The specification, several attempts at once (SPEC_VARIANTS): a fast navigator (thinking off) and careful
        ones (thinking on, more steps, each with its own strategy) work in separate contexts of the website; the
        first that yields a revision with requirements wins and the others are cancelled, as are all of them at
        the cutoff. Requirements stream to the ERP as soon as any attempt reads them, all from one revision (the
        first one read); requirements read before an attempt stopped are kept.
        """
        board = self.board
        erp = None
        if erp_site:
            if self._erp is None:
                self._erp = Erp(self.access[erp_site], self.contexts[erp_site], self.trace, self.research_deadline)
                self._erp.warm_up()
            erp = self._erp
        spares: list[BrowserContext] = []
        try:
            context = self.task["brief"].split("## Rules")[0].strip()
            names = [n for n in SPEC_VARIANTS if n in SPEC_STRATEGIES] or ["fast"]
            cutoff = min(self.research_deadline, self.started + SPEC_RACE_SECONDS)
            attempts: dict[asyncio.Task, str] = {}
            for i, name in enumerate(names):
                spare = await self._spare_context(site) if i else None
                if spare is not None:
                    spares.append(spare)
                attempts[asyncio.create_task(self._spec_attempt(
                    site, "spec" if i == 0 else f"spec{i + 1}", spare or self.contexts[site], context, erp,
                    strategy=name, deadline=cutoff, race=len(names) > 1, delay=SPEC_STAGGER * i))] = name
            results: list[SpecResult] = []
            pending = set(attempts)
            while pending and self.spec is None:
                done, pending = await asyncio.wait(pending, timeout=max(0.0, cutoff + SPEC_GRACE - time.monotonic()),
                                                   return_when=asyncio.FIRST_COMPLETED)
                if not done:  # the cutoff: what the attempts streamed so far stands
                    board.problem("specification", "no attempt finished before the cutoff")
                    break
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
                                         revision=result.current_revision, seconds=result.seconds,
                                         since_start=round(time.monotonic() - self.started, 1))
                    else:
                        for problem in result.problems:
                            board.problem("specification", f"{attempts[task]} attempt: {problem}")
            for task in pending:  # the other attempts are no longer needed
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for spare in spares:  # their tabs would compete with the ERP workers for the browser
                await self._close_context(spare)
            spares.clear()
            if self.spec is None:  # none finished with requirements: keep the most that was read
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
            # A requirement the ERP did not answer goes back to it at once, beside the mail research: waiting for
            # the mail first cost an eval run 216 s (search failed at 363 s, sent back at 579 s).
            await self.cover()
        except Exception as error:
            board.problem("specification", f"{type(error).__name__}: {str(error)[:200]}")
        finally:
            board.finish("requirement")
            board.finish("material")
            for spare in spares:
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
                        Source(site=site, url=url, quote=quote_for(seen, req.requirement_id,
                                                                    str(req.bore if req.bore is not None else ""))),
                        "specification")
        if erp:
            erp.start(self.effective(req))

    async def _spec_attempt(self, site: str, tag: str, context: BrowserContext, brief: str, erp: Erp | None, *,
                            strategy: str, deadline: float, race: bool = False, delay: float = 0.0) -> SpecResult:
        """One specification attempt with its strategy (thinking, steps, hint, model), in its own session."""
        if delay:
            await asyncio.sleep(delay)
        how = SPEC_STRATEGIES[strategy]
        access = {**self.access[site], "siteId": f"{site}:{tag}"}
        await self.site_up(site)
        session = await start_session(SiteSession(access, context, self.trace), until=self.research_deadline)
        self.trace.event("specification", "variant_start", variant=tag, strategy=strategy,
                         strong=bool(how["strong"] and llm.strong_model() is not None))
        return await research_spec(
            session, self.briefing, context=brief, deadline=deadline, thinking=how["thinking"],
            max_steps=how["steps"], hint=how["hint"], strong=how["strong"], race=race,
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

    async def _post_materials(self, site: str, only: set[str] | None = None) -> None:
        assert self.erp is not None
        for research in self.erp.requirements:
            if only is not None and research.requirement_id not in only:
                continue
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
            kinds = await self._plan_mail(sites, findings)
            self.mail_kinds = [k for k in kinds if k.site == site]
            elsewhere = [f"{k.name} on {k.site}" for k in kinds if k.site != site]
            if elsewhere:  # another mail site would be its own job (the plan names every website it needs)
                board.problem("mails", f"planned on other websites, not searched here: {elsewhere}")
            self.trace.event("mails", "plan", kinds=[f"{k.name}: {k.subject} / {k.sender}" for k in self.mail_kinds])
            await self._research_mail(self.mail_kinds, starts=starts, survey=True)
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

    async def _research_mail(self, kinds: list, deadline: float | None = None, starts: list | None = None,
                             survey: bool = False) -> None:
        """
        Read the record kinds from the mailbox until the mail deadline, then resolve whatever records were read: a
        kind still open at the deadline is dropped, the rest go to calc. With `survey`, the first tab that is up
        reads the mailbox once for all kinds (research_mail.survey); only the kinds it found no message for are
        then searched on their own, one kind per tab at a time, and only while something may refer to them.
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
        surveyed = asyncio.Event()  # the survey has sorted the rows (or there is none): single searches may start
        surveying: list[bool] = []
        listed: list[bool] = []  # set once the survey has listed the mailbox and sorted its rows
        if not (survey and MAIL_SURVEY and hasattr(research_mail, "survey")):
            surveyed.set()
        running: dict[str, list] = {}  # kind name -> [since, kind, the searches for it now running]
        unneeded: set[str] = set()  # kinds no source refers to: not searched (on), and not counted as open
        absent: set[str] = set()  # kinds a complete survey found no row for
        took: list[float] = []  # how long the searches that read a record took
        helped: set[str] = set()

        def straggler():
            """The kind longest in search on one tab alone, once that is much longer than the others took."""
            if len(took) < STRAGGLER_AFTER or deadline - time.monotonic() < MAIL_RETRY_SECONDS:
                return None
            slow = STRAGGLER * sorted(took)[len(took) // 2]
            late = [(since, kind) for name, (since, kind, copies) in running.items()
                    if name not in helped and name not in unneeded and len(copies) == 1
                    and time.monotonic() - since > slow]
            return min(late, key=lambda item: item[0])[1] if late else None

        async def search(session: SiteSession, kind, tries: int, helping: bool = False) -> None:
            budget = self._kind_seconds(deadline, queue.qsize())
            if kind.name in absent:
                budget = min(budget, ABSENT_SECONDS)
            options = {"seconds": budget} if "seconds" in KIND_OPTIONS else {}
            copy = asyncio.ensure_future(research_mail.research_kind(session, kind, self.reference,
                                                                     deadline=deadline, **options))
            entry = running.setdefault(kind.name, [time.monotonic(), kind, []])
            entry[2].append(copy)
            try:
                while not copy.done():
                    await asyncio.wait({copy}, timeout=2.0)
                    if not copy.done() and self.referred(kind.name) is False:
                        unneeded.add(kind.name)  # nothing refers to this record: its search has no use
                        self.trace.event(session.name, "mail_not_needed", record_kind=kind.name,
                                         after=round(time.monotonic() - entry[0]))
                        copy.cancel()
                found = await copy
            except asyncio.CancelledError:
                if not copy.cancelled() or asyncio.current_task().cancelling():  # this tab itself was stopped
                    copy.cancel()
                    raise
                return  # the same search on another tab read its record first, or nothing refers to the record
            except Exception as error:  # one failed kind must not cost the others
                self.board.problem("mails", f"{kind.name}: {type(error).__name__}: {str(error)[:160]}")
                return
            finally:
                entry[2].remove(copy)
                if not entry[2]:
                    running.pop(kind.name, None)
            if found.records:
                took.append(found.seconds)
                for other in entry[2]:  # the same search on another tab is no longer needed
                    other.cancel()
            earlier = self.mail_results.get(kind.name)
            if earlier is not None:  # a second try adds to what the first one read
                found.records = [*earlier.records, *found.records]
                found.kept = [*earlier.kept, *found.kept]
            self.mail_results[kind.name] = found
            stopped = getattr(found, "stopped", None)
            if stopped is None:  # out of its budget without reading a message
                stopped = not found.records and found.seconds >= min(budget, getattr(research_mail, "KIND_SECONDS", budget)) - 5
            if (stopped and not tries and not helping and not entry[2] and self.referred(kind.name) is not False
                    and kind.name not in absent and deadline - time.monotonic() > MAIL_RETRY_SECONDS):
                # Out of its budget: once more at the end of the queue, from a fresh start on this tab.
                self.board.problem("mails", f"{kind.name}: search stopped at its budget; tried once more")
                queue.put_nowait((kind, 1))

        async def look_once(session: SiteSession) -> None:
            """The survey on this tab: afterwards only the kinds it found no message for wait in the queue."""
            waiting: list = []
            while not queue.empty():
                waiting.append(queue.get_nowait())
            with_rows: set[str] = set()

            def sorted_out(found: set[str], complete: bool = True) -> None:  # the others need not wait for the reading
                with_rows.update(found)
                if complete:
                    listed.append(True)
                    absent.update(kind.name for kind, _ in waiting if kind.name not in found)
                for kind, tries in waiting:
                    if kind.name not in found:
                        queue.put_nowait((kind, tries))
                surveyed.set()

            results: dict = {}
            try:
                results = await research_mail.survey(session, [kind for kind, _ in waiting], self.reference,
                                                     deadline=deadline, on_assigned=sorted_out)
            except Exception as error:  # the single searches do the work, as before
                self.board.problem("mails", f"mailbox survey: {type(error).__name__}: {str(error)[:160]}")
            finally:
                if not surveyed.is_set():
                    sorted_out(set(), complete=False)
            for kind, tries in waiting:
                found = results.get(kind.name)
                if found is not None and found.records:
                    earlier = self.mail_results.get(kind.name)
                    if earlier is not None:
                        found.records = [*earlier.records, *found.records]
                        found.kept = [*earlier.kept, *found.kept]
                    self.mail_results[kind.name] = found
                    took.append(found.seconds)
                elif kind.name in with_rows:
                    queue.put_nowait((kind, tries))  # rows, but no record read from them: its own search

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
            if not surveyed.is_set() and not surveying:
                surveying.append(True)
                await look_once(session)
            await surveyed.wait()
            while time.monotonic() < deadline - 20:
                if not queue.empty():
                    kind, tries = queue.get_nowait()
                    need = self.referred(kind.name)
                    if need is False:
                        unneeded.add(kind.name)
                        self.trace.event(session.name, "mail_not_needed", record_kind=kind.name, after=0)
                        continue
                    if need is None and listed and self.awaited(kind.name) and kind.name not in self.mail_results:
                        # The survey listed the tender's messages and none is this record. A search of its own
                        # waits until something refers to the record: until then nobody knows it is needed at all.
                        queue.put_nowait((kind, tries))
                        await asyncio.sleep(1.0)
                        continue
                    await search(session, kind, tries)
                    continue
                slow = straggler()  # nothing waits: help a search that hangs on another tab
                if slow is not None:
                    helped.add(slow.name)
                    self.trace.event(session.name, "mail_help", record_kind=slow.name,
                                     running=round(time.monotonic() - running[slow.name][0]),
                                     others_took=sorted(took))
                    await search(session, slow, 1, helping=True)
                    continue
                if not running:
                    break
                await asyncio.sleep(1.0)

        tabs = [asyncio.create_task(tab(s)) for s in (starts or list(self.mail_sessions))]
        _, late = await asyncio.wait(tabs, timeout=max(1.0, deadline - time.monotonic()))
        for job in late:
            job.cancel()
        for start in starts or []:
            if not start.done():
                start.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)
        open_kinds = [k.name for k in kinds if k.name not in self.mail_results and k.name not in unneeded]
        if open_kinds:
            self.board.problem("mails", f"mail deadline: {open_kinds} not read; their fields stay blank")
        if not self.mail_sessions:
            raise ConnectionError("no mail tab could be opened")
        mailbox = await address[0] if address else await self._mailbox()
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
                            ("commitments", "commitments"), ("fx_rates", "fx"), ("setup_pools", "setup_pools"),
                            ("project_bonuses", "project_bonus")):
            for key, value in (getattr(facts, table, None) or {}).items():
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
            jobs += [asyncio.create_task(self.tender(site), name=f"tender@{site}") for site in sites["tender"]]
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
        """
        The verifier judges the facts; a fact whose record it doubts (another reference, a later date, a wrong
        sender, a superseded record) goes back to its worker once, and the reworked facts are judged again. A
        doubt that is only about the quote (missing, or a header that does not show the value) is recorded but
        sends nothing back: a new search would post the same quote, and cost its 150 s for nothing.
        """
        doubtful = await verify_board(self.board, self.rules, self.reference, self.briefing.scenario_date)
        by_worker: dict[str, list] = {}
        for fact in doubtful:
            if getattr(fact, "rework", True):
                by_worker.setdefault(fact.worker, []).append(fact)
        if len(doubtful) > sum(len(facts) for facts in by_worker.values()):
            self.trace.event("verify", "evidence_only",
                             facts=[f.ident for f in doubtful if not getattr(f, "rework", True)][:30])
        reworked = False
        for worker, facts in by_worker.items():
            rework = self.rework.get(worker)
            if not REWORK:
                self.trace.event("verify", "rework_off", by=worker, facts=[f.ident for f in facts])
                continue
            if rework is None or self.left() < REWORK_SECONDS:
                continue
            self.trace.event("verify", "rework", by=worker, facts=[f.ident for f in facts])
            reworked = True
            try:
                await asyncio.wait_for(rework(facts), max(1.0, self.left()))
            except Exception as error:
                self.board.problem("verify", f"rework {worker}: {type(error).__name__}: {str(error)[:160]}")
        if reworked:
            await verify_board(self.board, self.rules, self.reference, self.briefing.scenario_date)

    def uncovered(self) -> list[str]:
        """The requirements whose ERP findings are not complete: no findings at all, a search that listed no
        material without the website saying the group has none, or listed materials that were not all read."""
        if self.erp is None:
            return []
        found = {r.requirement_id: r for r in self.erp.requirements}
        missing = []
        for rid in self.requirements:
            research = found.get(rid)
            if not getattr(self.requirements[rid], "catalog_group", "x").strip():
                continue  # no catalogue group named: the ERP has nothing to be asked again
            if research is None:
                missing.append(rid)
            elif not research.no_hits and (not research.inspected
                                           or any(n not in self.erp.materials for n in research.inspected)):
                missing.append(rid)
            elif self.status_guessed(research):
                missing.append(rid)
        return missing

    def status_guessed(self, research) -> list[str]:
        """The requirement's materials that were called inactive although no view said what their status means,
        and that were not read a second time yet. Such a material is a guess: in an eval the one material that
        fitted was read as inactive in one run and as active in the next, and the guess cost its position."""
        again = getattr(self, "reread", set())
        out = []
        for number in research.inspected:
            record = self.erp.materials.get(number)
            if (number not in again and record is not None and not getattr(record, "active", True)
                    and "status meaning not read" in (getattr(record, "problems", None) or [])):
                out.append(number)
        return out

    async def cover(self) -> None:
        """Send the requirements that fail the coverage check back to the ERP (see COVERAGE_ROUNDS). Nothing
        happens, and nothing is spent, when every requirement is covered."""
        erp = self._erp
        if erp is None or self.erp is None or not hasattr(erp, "so_far"):
            return
        sent: set[str] = set()
        # called when the ERP research ends and again after all research: the rounds are counted over both
        for attempt in range(getattr(self, "cover_rounds", 0) + 1, COVERAGE_ROUNDS + 1):
            missing = self.uncovered()
            if not missing or self.left() < COVERAGE_SECONDS:
                break
            self.cover_rounds = attempt
            self.trace.event("coverage", "sent_back", round=attempt, requirements=missing,
                             seconds_left=round(self.left()))
            sent.update(missing)
            for number, task in list(erp.materials.items()):  # a material that was not read is read afresh
                if task.done() and (task.cancelled() or task.exception() is not None or task.result() is None):
                    del erp.materials[number]
            self.reread = getattr(self, "reread", set())
            for research in self.erp.requirements:  # and one whose status was a guess, once
                if research.requirement_id in missing:
                    for number in self.status_guessed(research):
                        erp.materials.pop(number, None)
                        self.reread.add(number)
            tasks = []
            for rid in missing:
                earlier = erp.tasks.pop(rid, None)
                if earlier is not None and not earlier.done():
                    earlier.cancel()
                erp.partial.pop(rid, None)
                tasks.append(erp.start(self.effective(self.requirements[rid])))
            await asyncio.wait(tasks, timeout=max(1.0, self.left()))
            for task in [*tasks, *erp.materials.values()]:  # nothing goes on browsing beside the bid form
                if not task.done():
                    task.cancel()
            self.erp = erp.so_far()
            await self._post_materials(erp.site["siteId"], only=set(missing))
        if sent:
            still = self.uncovered()
            self.trace.event("coverage", "done", covered=sorted(sent - set(still)), still_missing=still)

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

    def _unknown_clauses(self, p, clause, pool, bonus) -> None:
        """A position whose Q&A names a key the mails did not resolve: calc would report a zero for that part,
        which reads as a known zero, so the report field and the unit price it feeds are left blank instead."""
        fields = set()
        if getattr(p, "packaging_key", None) and getattr(clause, "packaging", None) is None:
            fields |= {"packaging_per_unit", "unit_price"}
        if getattr(p, "setup_pool_key", None) and p.position not in getattr(pool, "members", []):
            fields |= {"surcharge_per_unit", "unit_price"}
        if getattr(p, "bonus_key", None) and p.position not in getattr(bonus, "members", []):
            fields |= {"rebate_total", "unit_price"}
        if fields:
            self.blank_hints.setdefault(p.position, set()).update(fields)

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
                unread = unread_attributes(research, self.erp.materials if self.erp else {})
                if pending or (research is not None and research.problems) or unread:
                    problems.append(f"position {p.position}: material undecided "
                                    f"({'; '.join(pending or research.problems or unread)[:160]}); position omitted")
                    continue
            material = self.erp.materials.get(number) if (number and self.erp) else None
            if number and material is None:  # chosen, but its record was not read: a gap, never "no material"
                problems.append(f"position {p.position}: record of {number} not read; position omitted")
                continue
            if material is not None:
                self.attributes[p.position] = material_attributes(material)
                group = getattr(self.requirements.get(req_id), "catalog_group", "") if req_id else ""
                if group:  # a form may ask for the catalogue group before it offers the group's materials
                    self.attributes[p.position]["catalog_group"] = group
            clause = by_position.get(p.position)
            self._unknown_clauses(p, clause, getattr(clauses, "setup_pool", None),
                                  getattr(clauses, "project_bonus", None))
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
                            setup_pool=getattr(clauses, "setup_pool", None),
                            project_bonus=getattr(clauses, "project_bonus", None))
        return bid, problems

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

    # Which mail record kind resolves which key that another source refers to: (kind, table of the mail facts).
    REFERENCED = {"condition_group": ("condition_rules", "discount_scopes"),
                  "packaging_key": ("packaging", "packaging"),
                  "commitment_key": ("commitments", "commitments"),
                  "setup_pool_key": ("setup_pools", "setup_pools"),
                  "bonus_key": ("project_bonus", "project_bonuses")}

    def referred(self, kind: str) -> bool | None:
        """
        Whether anything refers to a mail record of this kind: a key the tender's Q&A gave a position, a reserve
        the specification asks for, an index or exchange-rate key on a material. None while the source that would
        refer to it is not read to its end; False only once it is and names none. Kinds no key leads to (the
        approval) are always wanted. A record nothing refers to cannot enter the calculation, so its search is
        time without a result: one world had mail for the approval only, and seven other searches ran 150 s each,
        twice.
        """
        fields = [field for field, (name, _) in self.REFERENCED.items() if name == kind]
        if fields:
            facts = self.tender_facts
            positions = getattr(facts, "positions", None) or []
            if any(getattr(p, field, None) for p in positions for field in fields):
                return True
            done = self.board.finished("position") and bool(positions) and getattr(facts, "qa_complete", True)
            return False if done else None
        if kind == "end_reserves":
            if any(getattr(r, "reserve_key", None) or getattr(r, "stroke_is_usable", False)
                   for r in self.requirements.values()):
                return True
            return False if self.board.finished("requirement") and self.requirements else None
        if kind in ("metal_indices", "fx"):
            records = list(self.erp.materials.values()) if self.erp else []
            if kind == "metal_indices" and any(getattr(r, "metal_index", None) for r in records):
                return True
            if kind == "fx" and any(getattr(c, "fx_key", None) or str(getattr(c, "currency", "EUR")).upper() != "EUR"
                                    for r in records for c in getattr(r, "conditions", None) or []):
                return True
            return False if self.board.finished("material") and records else None
        return True

    def awaited(self, kind: str) -> bool:
        """Whether the source that would refer to a record of this kind is still being read (see `referred`)."""
        if any(name == kind for name, _ in self.REFERENCED.values()):
            return not self.board.finished("position")
        if kind == "end_reserves":
            return not self.board.finished("requirement")
        if kind in ("metal_indices", "fx"):
            return not self.board.finished("material")
        return False

    def unresolved_references(self) -> dict[str, set[str]]:
        """
        Mail record kind -> the keys other sources refer to that no record of that kind resolved: the keys the
        tender's Q&A assigns to positions, the reserve keys of the specification, and the index and exchange-rate
        keys of the materials read. A key that is referred to must resolve; one that does not means a message was
        not found or not read, and that is known here without any model's opinion.
        """
        facts = self.mail_facts
        if facts is None:
            return {}
        wanted: dict[tuple[str, str], set[str]] = {}
        for p in getattr(self.tender_facts, "positions", None) or []:
            for field, target in self.REFERENCED.items():
                if getattr(p, field, None):
                    wanted.setdefault(target, set()).add(str(getattr(p, field)))
        for req in self.requirements.values():
            if getattr(req, "reserve_key", None):
                wanted.setdefault(("end_reserves", "end_reserves"), set()).add(str(req.reserve_key))
        for record in (self.erp.materials.values() if self.erp else []):
            if getattr(record, "metal_index", None):
                wanted.setdefault(("metal_indices", "metal_indices"), set()).add(str(record.metal_index))
            for condition in getattr(record, "conditions", None) or []:
                if getattr(condition, "fx_key", None):
                    wanted.setdefault(("fx", "fx_rates"), set()).add(str(condition.fx_key))
        out: dict[str, set[str]] = {}
        for (kind, table), keys in wanted.items():
            have = {calc.norm_code(k) for k in (getattr(facts, table, None) or {})}
            missing = {k for k in keys if calc.norm_code(k) not in have}
            if missing:
                out[kind] = missing
        return out

    def _resolve_mail_again(self) -> None:
        domain = self.mailbox.split("@", 1)[1] if "@" in self.mailbox else ""
        self.mail_facts = research_mail.resolve_mail(list(self.mail_results.values()), self.reference, domain,
                                                     self.scenario, self.mailbox)

    async def resolve_references(self) -> None:
        """
        Keys that are referred to but did not resolve: first the messages kept for that kind are read once more
        (a reading can miss the record or its entries), then, if keys are still open and time allows, that kind
        alone is searched again. Nothing happens, and nothing is spent, when every key resolved.
        """
        if research_mail is None or self.mail_facts is None:
            return
        open_keys = self.unresolved_references()
        if not open_keys:
            return
        self.trace.event("mails", "unresolved_keys", **{kind: sorted(keys) for kind, keys in open_keys.items()})

        async def read_again(result) -> None:
            records = await asyncio.gather(*(research_mail.read_message(view, result.kind, self.reference)
                                             for view in dict.fromkeys(result.kept)), return_exceptions=True)
            result.records = [*result.records, *(r for r in records if r is not None
                                                 and not isinstance(r, BaseException))]

        again = [self.mail_results[kind] for kind in open_keys
                 if kind in self.mail_results and self.mail_results[kind].kept]
        if again:
            await asyncio.gather(*(read_again(result) for result in again))
            self._resolve_mail_again()
            open_keys = self.unresolved_references()
            self.trace.event("mails", "read_again", kinds=[r.kind.name for r in again],
                             still_open={kind: sorted(keys) for kind, keys in open_keys.items()})
        kinds = [k for k in self.mail_kinds if k.name in open_keys]
        if kinds and self.mail_sessions and self.left() > MAIL_RETRY_SECONDS:
            self.trace.event("mails", "search_again", kinds=[k.name for k in kinds])
            await self._research_mail(kinds, min(self.research_deadline, time.monotonic() + REWORK_SECONDS))
            self.trace.event("mails", "after_search_again",
                             still_open={kind: sorted(keys) for kind, keys in self.unresolved_references().items()})

    async def second_reading(self, missing: list[str]) -> dict[str, str]:
        """
        Before a no bid is accepted: every candidate of the positions without material is read a second time from
        the views kept for it, by a reader that thinks. Where the two readings differ on whether the material
        qualifies (active, every attribute equal), a third decides. Returns requirement -> material for those that
        qualify after all. A whole bid must not fall to one misread status code or one misread number: in the
        sandbox that happened once in five runs.
        """
        erp = self._erp
        if self.erp is None or erp is None or not hasattr(erp, "reread"):
            return {}
        positions = {p.position: p for p in getattr(self.tender_facts, "positions", []) or []}
        found: dict[str, str] = {}

        async def settle(req_id: str, research, number: str) -> None:
            want = self.effective(self.requirements[req_id])

            def fits(record) -> bool:
                return bool(record is not None and record.active
                            and not mismatches(want, merged_attributes(record, research)))

            first = self.erp.materials.get(number)
            second = await erp.reread(number)
            if second is None or fits(first) == fits(second):
                return
            third = await erp.reread(number)
            self.trace.event("verify", "second_reading", requirement=req_id, material=number, first=fits(first),
                             second=fits(second), third=fits(third) if third is not None else None)
            if third is not None and fits(third) and fits(second):  # two careful readings against the first
                self.erp.materials[number] = second
                found.setdefault(req_id, number)

        jobs = []
        for position in missing:
            p = positions.get(position)
            req_id = next((rid for rid in self.requirements if _same_id(rid, p.requirement)), None) if p else None
            research = next((r for r in self.erp.requirements if r.requirement_id == req_id), None)
            if req_id is None or research is None or req_id in {job[0] for job in jobs}:
                continue
            jobs += [(req_id, research, number) for number in research.inspected]
        if jobs:
            await asyncio.gather(*(settle(*job) for job in jobs), return_exceptions=True)
        return found

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
        await self._bounded(self.cover(), "coverage")
        if not REWORK:
            if VERIFY_LOG:  # for the run log only: nothing waits for it
                self.verifying = asyncio.create_task(self._bounded(self.verify(), "verify"))
        elif self.form_left() > 60:
            await self._bounded(self.verify(), "verify")
        await self._bounded(self.resolve_references(), "references")
        await self._bounded(self.select_materials(), "select")
        result, blank, problems = self._calculate()
        if result is not None and result.decision == "no_bid" and result.missing_materials and self.form_left() > 45:
            # Before a no_bid is accepted: a second reading of the candidates (one misread status or number must
            # not cost the whole bid) ...
            try:
                reread = await asyncio.wait_for(self.second_reading(result.missing_materials),
                                                max(1.0, min(VERIFY_SECONDS, self.form_left() - 30)))
            except Exception as error:
                reread = {}
                self.board.problem("verify", f"second reading: {type(error).__name__}: {str(error)[:160]}")
            if reread:
                self.chosen = {**self.chosen, **reread}
                again = self._calculate()
                self.trace.event("verify", "reread_changed", chosen=reread,
                                 decision=again[0].decision if again[0] is not None else None)
                if again[0] is not None:
                    result, blank, problems = again
        if result is not None and result.decision == "no_bid" and result.missing_materials and self.form_left() > 45:
            # ... and are the mismatches that leave positions without material only wording?
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
