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
from evidence import EffectiveRequirement, Requirement, effective_requirement, parse_date, to_position_input
from research import Erp, ErpResearch, SpecResult, research_spec, reselect
from tracing import Trace
from verify import confirm_decision, verify_board

try:  # the pipeline degrades without them
    import research_tender
except ImportError:  # pragma: no cover
    research_tender = None
try:
    import research_mail
except ImportError:  # pragma: no cover
    research_mail = None

SUBMIT_SECONDS = float(os.environ.get("AGENT_SUBMIT_SECONDS", "480"))  # research ends this long before the end
FORM_SECONDS = float(os.environ.get("AGENT_FORM_SECONDS", "300"))  # the bid form and signing need this much
MAIL_TABS = int(os.environ.get("MAIL_TABS", "4"))
SPEC_RETRY_SECONDS = 240.0  # research time a second specification attempt needs to still feed the ERP
SELECT_SECONDS = 75.0  # the final material choice (a model call per requirement, in parallel)
MAIL_TAB_STAGGER = 4.0  # seconds between the mail tabs' sign-ins
MAIL_SECONDS = float(os.environ.get("AGENT_MAIL_SECONDS", "540"))  # from the orchestrator's start
FINDINGS_WAIT = 45.0  # how long the mail planner waits for the tender's keys before planning from the brief alone
REWORK_SECONDS = 150.0  # a doubtful fact goes back to its worker only with at least this much research time left


async def start_session(session: SiteSession) -> SiteSession:
    """Open and sign in (SiteSession.start: time-limited, tried twice)."""
    await session.start()
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
            message = str(error)
        culprit, why = None, message
        for p in positions:
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
    answer = result.answer(notes or None)
    for position in answer["positions"]:
        for field in blank.get(position["position"], ()):
            position.pop(field, None)
    return answer


# ---------------------------------------------------------------- the orchestrator


class Orchestrator:
    def __init__(self, task: dict, briefing: Briefing, contexts: dict[str, BrowserContext], trace: Trace,
                 deadline: float):
        self.task, self.briefing, self.contexts, self.trace = task, briefing, contexts, trace
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
        self.mail_facts = None
        self.spec: SpecResult | None = None
        self.erp: ErpResearch | None = None
        self._erp: Erp | None = None
        self.requirements: dict[str, Requirement] = {}
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

    async def session(self, site: str, tag: str = "") -> SiteSession:
        access = self.access[site]
        if tag:
            access = {**access, "siteId": f"{site}:{tag}"}
        return await start_session(SiteSession(access, self.contexts[site], self.trace))

    # ------------------------------------------------------------ workers

    async def tender(self, site: str) -> None:
        board = self.board
        try:
            session = await self.session(site, "tender")
            facts = await research_tender.research_tender(session, self.reference, self.scenario,
                                                          deadline=self.research_deadline)
            if facts is None:
                board.problem("tender", "no positions found")
                return
            self.tender_facts = facts
            for p in facts.positions:
                value = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in p.model_dump(mode="json").items()}
                board.post("position", p.position, value,
                           Source(site=site, url=session.page.url,
                                  quote=quote_for(session.seen, p.position, str(p.tender_quantity))
                                  or quote_for(session.seen, p.position)), "tender")
            for a in getattr(facts, "amendments", []):
                key = f"{a.revision}/{a.requirement}/{'+'.join(a.attributes)}"
                board.post("amendment", key, {"date": a.date.isoformat(), **{k: str(v) for k, v in a.attributes.items()}},
                           Source(site=site, quote=quote_for(session.seen, a.requirement, *map(str, a.attributes.values()))),
                           "tender")
            for number in getattr(facts, "withdrawn", []):
                board.post("withdrawn", number, number, Source(site=site, quote=quote_for(session.seen, number)),
                           "tender")
        except Exception as error:
            board.problem("tender", f"{type(error).__name__}: {str(error)[:200]}")
        finally:
            for kind in ("position", "amendment", "withdrawn"):
                board.finish(kind)

    def _amendments(self) -> list[calc.Amendment]:
        return list(getattr(self.tender_facts, "amendments", []) or [])

    def _reserves(self, revision: str) -> dict[str, Decimal]:
        """Per-end reserves by key, only from records for the requirement's own spec revision (they are
        revision-specific); a record that names no revision counts for any."""
        reserves = getattr(self.mail_facts, "end_reserves", None) or {}
        return {key: Decimal(str(getattr(entry, "reserve_per_end_mm", entry))) for key, entry in reserves.items()
                if not getattr(entry, "revision", "") or _same_id(getattr(entry, "revision", ""), revision)}

    def effective(self, req: Requirement) -> EffectiveRequirement:
        return effective_requirement(req, amendments=self._amendments(), reserves=self._reserves(req.revision),
                                     scenario_date=self.scenario_stamp)

    async def specification(self, site: str, erp_site: str | None) -> None:
        board = self.board
        erp = None
        if erp_site:
            if self._erp is None:
                self._erp = Erp(self.access[erp_site], self.contexts[erp_site], self.trace, self.research_deadline)
                self._erp.warm_up()
            erp = self._erp
        try:
            session = await self.session(site, "spec")
            context = self.task["brief"].split("## Rules")[0].strip()

            def on_requirement(req: Requirement) -> None:
                if req.requirement_id in self.requirements:
                    return
                self.requirements[req.requirement_id] = req
                board.post("requirement", req.requirement_id, req.raw or req.model_dump(mode="json"),
                           Source(site=site, url=session.page.url,
                                  quote=quote_for(session.seen, req.requirement_id, str(req.bore))), "specification")
                if erp:
                    erp.start(self.effective(req))

            self.spec = await research_spec(session, self.briefing, context=context, deadline=self.research_deadline,
                                            on_requirement=on_requirement)
            if not self.spec.requirements and self.left() > SPEC_RETRY_SECONDS:
                # The fast (thinking off) navigator sometimes loses its way in the document list: once more, from a
                # fresh tab, thinking.
                for problem in self.spec.problems:
                    board.problem("specification", f"first attempt: {problem}")
                session = await self.session(site, "spec2")
                self.spec = await research_spec(session, self.briefing, context=context,
                                                deadline=self.research_deadline, on_requirement=on_requirement,
                                                thinking=True)
            for req in self.spec.requirements:
                on_requirement(req)
            for problem in self.spec.problems:
                board.problem("specification", problem)
            if erp is None:
                return
            self.erp = await erp.collect()
            await self._post_materials(erp_site or "")
        except Exception as error:
            board.problem("specification", f"{type(error).__name__}: {str(error)[:200]}")
        finally:
            board.finish("requirement")
            board.finish("material")

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
            positions = await board.wait("position", timeout=min(FINDINGS_WAIT, max(1.0, self.left())))
            findings = "\n".join(f"- position {k}: " + ", ".join(
                f"{f} {v}" for f, v in fact.value.items() if f.endswith("_key") or f == "condition_group")
                for k, fact in positions.items())
            sites = {s["siteId"]: s["url"] for s in self.task["access"]}
            kinds = await asyncio.wait_for(research_mail.plan_searches(self.task["brief"], sites, findings),
                                           max(1.0, self.mail_deadline - time.monotonic()))
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

    async def _mail_tab(self, site: str, i: int) -> SiteSession:
        await asyncio.sleep(MAIL_TAB_STAGGER * i)
        return await self.session(site, f"mail{i + 1}")

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
        for kind in kinds:
            queue.put_nowait(kind)
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
                kind = queue.get_nowait()
                try:
                    self.mail_results[kind.name] = await research_mail.research_kind(
                        session, kind, self.reference, deadline=deadline)
                except Exception as error:  # one failed kind must not cost the others
                    self.board.problem("mails", f"{kind.name}: {type(error).__name__}: {str(error)[:160]}")

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
                def finished(tasks):
                    return {k: t.result() for k, t in tasks.items()
                            if t.done() and not t.cancelled() and t.exception() is None and t.result() is not None}
                self.erp = ErpResearch(requirements=list(finished(self._erp.tasks).values()),
                                       materials=finished(self._erp.materials))
        self.rework = {"mails": self._rework_mail}

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
        try:  # one hung model call must not hold the bid; the ERP worker's own choices stand in
            self.chosen = await asyncio.wait_for(reselect(effective, self.erp, self.trace), SELECT_SECONDS)
        except Exception as error:
            self.board.problem("select", f"reselect: {type(error).__name__}; the ERP workers' choices are used")
            self.chosen = {r.requirement_id: r.selected for r in self.erp.requirements}
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
            if number is None and (research is None or not research.inspected):
                problems.append(f"position {p.position}: ERP not researched; position omitted")
                continue
            if number is None:
                # No material only means no_bid (rule 3) when the search for it was complete; a requirement still
                # pending (e.g. a reserve not found) or a search that hit problems leaves the position out instead.
                pending = self.effective(self.requirements[req_id]).pending
                if pending or (research is not None and research.problems):
                    problems.append(f"position {p.position}: material undecided "
                                    f"({'; '.join(pending or research.problems)[:160]}); position omitted")
                    continue
            material = self.erp.materials.get(number) if (number and self.erp) else None
            if material is not None:
                self.attributes[p.position] = material_attributes(material)
            clause = by_position.get(p.position)
            self._unknown_clauses(p, clause, getattr(clauses, "setup_pool", None),
                                  getattr(clauses, "project_bonus", None))
            extra = ({"discount_scope": clause.discount_scope, "setup_fee": clause.setup_fee,
                      "packaging": clause.packaging, "commitment": clause.commitment} if clause
                     else {"setup_fee": getattr(p, "setup_fee", Decimal(0))})
            try:
                positions.append(to_position_input(p.position, p.quantity, p.unit, self.requirements[req_id],
                                                   material, **extra))
            except calc.CalcError as error:
                problems.append(f"position {p.position}: {error}")
                positions.append(to_position_input(p.position, p.quantity, p.unit, self.requirements[req_id], None,
                                                   **extra) if material is None else
                                 calc.PositionInput(position=p.position, quantity=p.quantity,
                                                    unit=p.unit if isinstance(p.unit, calc.Unit) else calc.parse_unit(p.unit),
                                                    pieces_per_set=self.requirements[req_id].pieces_per_set, **extra))
        bid = calc.BidInput(tender_reference=self.reference, scenario_date=self.scenario_stamp, positions=positions,
                            approval=getattr(clauses, "approval", None) or getattr(self.mail_facts, "approval", None),
                            fx_rates=getattr(clauses, "fx_rates", None) or getattr(self.mail_facts, "fx_rates", {}) or {},
                            metal_indices=getattr(clauses, "metal_indices", None)
                            or getattr(self.mail_facts, "metal_indices", {}) or {},
                            setup_pool=getattr(clauses, "setup_pool", None),
                            project_bonus=getattr(clauses, "project_bonus", None))
        return bid, problems

    async def run(self) -> tuple[dict | None, calc.BidResult | None]:
        """Research, verify, calculate: the answer dict for the bid form (None when nothing could be built)."""
        await self.research()
        # The deadline guard after research: each step runs only in the time the bid form leaves it.
        if self.form_left() > 60:
            await self._bounded(self.verify(), "verify")
        await self._bounded(self.select_materials(), "select")
        bid, problems = self.build_bid()
        self.trace.event("calc", "input", positions=[p.position for p in bid.positions], problems=problems)
        if not bid.positions:
            self.board.problem("calc", "no position could be built; the bid form gets no positions")
            return None, None
        result, blank, calc_problems = safe_calculate(bid)
        for position, fields in self.blank_hints.items():
            blank.setdefault(position, set()).update(fields)
        problems += calc_problems
        if result is None:
            return None, None
        check = None if self.form_left() < 30 else await confirm_decision(result.decision, self.board.summary() + "\n\nProblems:\n"
                                       + "\n".join(self.board.problems + problems), self.rules)
        if check is not None and not check.agrees:
            self.trace.event("verify", "decision_disputed", proposed=result.decision, wanted=check.decision,
                             reason=check.reason)
        if check is not None and check.decision == "no_bid" and result.decision == "bid" and result.missing_materials:
            result = result.model_copy(update={"decision": "no_bid", "positions": [], "total_net": Decimal("0.00")})
        answer = answer_from(result, blank)
        self.trace.event("calc", "answer", answer=answer, problems=problems, blank={k: sorted(v) for k, v in blank.items()})
        return answer, result
