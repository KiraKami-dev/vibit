"""
The gap-filling second pass: after the first calculation, list what the answer still lacks, retry exactly that
while research time is left, recalculate, and repeat.

A gap is something a field or a position waits on: a requirement the specification read did not yield, an ERP
search that returned nothing or no decision, a material record missing facts, a mail record kind whose key the Q&A
names but no record resolved (approval, condition rules, metal index, stroke reserve, packaging, commitments, FX,
setup pool, project bonus), or no tender positions at all. Verifier doubts are listed too (traced only; verify
already sends doubtful mail facts back once).

The retries are the same model-driven workers as the first pass, told what is missing (a spec navigator gets the
missing requirement ids, a mail search gets its record kind on a fresh tab, the ERP worker its requirement);
this module only decides which retries to run, runs them side by side and bounds them by the research deadline.
It never removes a fact: results add to what the first pass found, and the more complete answer is kept.

Nothing here names a site, a label or a value of any dataset.
"""
import asyncio
import dataclasses
import inspect
import os
import time
from dataclasses import dataclass, field

GAP_MIN_ROUND = float(os.environ.get("AGENT_GAP_MIN_SECONDS", "75"))  # a round starts only with this much time
GAP_TAIL = 30.0  # rounds end this long before the research deadline: the reselection and calc come after
GAP_TRIES = 2  # retries per gap over the whole pass
GAP_ROUNDS = 3
FRESH_TAB_SECONDS = 60.0
MAIL_KEYS = {  # a position's key from the Q&A -> the mail record kind that resolves it, and the table it lands in
    "condition_group": ("condition_rules", "discount_scopes"),
    "packaging_key": ("packaging", "packaging"),
    "commitment_key": ("commitments", "commitments"),
    "setup_pool_key": ("setup_pools", "setup_pools"),
    "bonus_key": ("project_bonus", "project_bonuses"),
}


@dataclass(frozen=True)
class Gap:
    area: str  # tender, requirement, erp, material, mail, doubt
    key: str  # what to retry: requirement id, material number, mail record kind, fact id
    reason: str
    positions: tuple[str, ...] = ()

    @property
    def ident(self) -> str:
        return f"{self.area}/{self.key}"


@dataclass
class Calculation:
    """One calculation of the answer: calc's result, the report fields left blank, problems, form attributes."""
    result: object
    blank: dict[str, set[str]]
    problems: list[str]
    attributes: dict[str, dict[str, str]] = field(default_factory=dict)

    def completeness(self) -> tuple[int, int]:
        positions = len(getattr(self.result, "positions", []) or [])
        return positions, -sum(len(fields) for fields in self.blank.values())

    @property
    def decision(self) -> str | None:
        return getattr(self.result, "decision", None)


def _same_id(a: str, b: str) -> bool:
    a, b = " ".join(str(a).split()).casefold(), " ".join(str(b).split()).casefold()
    return a == b or (bool(a) and bool(b) and (a.endswith(b) or b.endswith(a)))


def _incomplete(record) -> list[str]:
    from research import incomplete
    return incomplete(record)


DOUBT = "verifier doubt"


def detect_gaps(o, calc_problems: list[str] = (), doubts: bool = False) -> list[Gap]:
    """
    What the answer still lacks, from the orchestrator's state (duck-typed: tender_facts, requirements, erp,
    chosen, mail_facts, board, effective()) and the calculation's problems. One gap per thing to retry; a gap that
    several positions wait on lists them all. A complete ERP search without a match is not a gap (rule 3: no_bid).
    """
    gaps: dict[str, Gap] = {}

    def add(area: str, key: str, reason: str, position: str | None = None) -> None:
        ident = f"{area}/{key}"
        old = gaps.get(ident)
        positions = tuple(dict.fromkeys([*(old.positions if old else ()), *([position] if position else [])]))
        gaps[ident] = Gap(area, key, old.reason if old else reason, positions)

    needs = {n.kind for n in getattr(getattr(o, "plan", None), "needs", [])}
    mail = getattr(o, "mail_facts", None)
    positions = list(getattr(getattr(o, "tender_facts", None), "positions", None) or [])
    tender = getattr(o, "tender_facts", None)
    if not positions and (not needs or "tender" in needs):
        add("tender", "positions", "no tender positions read")
    elif tender is not None and getattr(tender, "qa_complete", True) is False:
        # the Q&A worker stopped before the whole list was collected: answers (amendments, quantity changes,
        # keys) may be missing without any field showing it
        add("tender", "qa", "bidder Q&A not read to its end")
    if getattr(mail, "approval", None) is None and (not needs or "mails" in needs):
        add("mail", "approval", "no discount approval read")
    erp = getattr(o, "erp", None)
    by_rid = {r.requirement_id: r for r in (getattr(erp, "requirements", None) or [])}
    materials = getattr(erp, "materials", None) or {}
    chosen = getattr(o, "chosen", {}) or {}
    for p in positions:
        rid = next((r for r in o.requirements if _same_id(r, p.requirement)), None)
        if rid is None:
            add("requirement", p.requirement, "requirement not read from the specification", p.position)
        else:
            research = by_rid.get(rid)
            number = chosen[rid] if rid in chosen else getattr(research, "selected", None)
            if number is None:
                pending = o.effective(o.requirements[rid]).pending
                if pending:
                    add("mail", "end_reserves", f"stroke reserve pending: {'; '.join(pending)}", p.position)
                elif getattr(research, "no_hits", False) and not research.inspected:
                    pass  # the site reported the catalogue group empty: searched without a match (rule 3)
                elif research is None or not research.inspected:
                    add("erp", rid, "ERP not researched" + (f": {research.problems[0]}" if research and
                                                            research.problems else ""), p.position)
                elif research.problems:
                    add("erp", rid, f"material undecided: {'; '.join(research.problems)[:160]}", p.position)
            else:
                record = materials.get(number)
                if record is None:
                    add("material", number, "material record not read", p.position)
                else:
                    missing = _incomplete(record)
                    if not getattr(p, "packaging_key", None):
                        missing = [m for m in missing if "carton" not in m]
                    if not (getattr(p, "setup_pool_key", None) or getattr(record, "metal_index", None)):
                        missing = [m for m in missing if "mass" not in m]
                    if missing:
                        add("material", number, f"missing: {', '.join(missing)}", p.position)
        for attr, (kind, table) in MAIL_KEYS.items():
            key = getattr(p, attr, None)
            if key and key not in (getattr(mail, table, None) or {}):
                add("mail", kind, f"{attr} {key} unresolved", p.position)
    for problem in calc_problems:
        text = problem.casefold()
        position = problem.split()[1].rstrip(":") if problem.startswith("position ") else None
        if "fx rate" in text:
            add("mail", "fx", problem[:160], position)
        elif "metal index" in text and "code needed" not in text:
            add("mail", "metal_indices", problem[:160], position)
    board = getattr(o, "board", None)
    for fact in (getattr(board, "facts", None) or {}).values():
        if fact.verdict and fact.verdict != "ok":
            add("doubt", fact.ident, fact.verdict[:160])
            if doubts and fact.worker == "mails":  # its record kind is searched once more (verify's rework)
                add("mail", fact.key.split("/")[0], f"{DOUBT}: {fact.verdict[:140]}")
    return list(gaps.values())


def decisive(gaps: list[Gap], calculation: Calculation | None) -> list[Gap]:
    """
    For a no_bid on established evidence (positions whose complete ERP search found no matching material) only
    the gaps that can change it or the no_bid answer: the tender's Q&A (an amendment can bring a match back) and
    the discount approval. Masses, prices, keys and the other positions' materials only price a bid; retrying
    them held no_bid answers back by minutes.
    """
    result = getattr(calculation, "result", None)
    if getattr(result, "decision", None) != "no_bid" or not getattr(result, "missing_materials", None):
        return gaps
    return [g for g in gaps if g.area in ("tender", "doubt") or (g.area == "mail" and g.key == "approval")]


def better(new: Calculation, old: Calculation | None) -> bool:
    """The recalculation replaces the earlier answer unless it is less complete for the same decision (facts only
    accumulate, so that would mean a retry made things worse); a changed decision rests on new evidence."""
    if old is None or old.result is None:
        return new.result is not None or old is None
    if new.result is None:
        return False
    return new.decision != old.decision or new.completeness() >= old.completeness()


class GapFiller:
    """Runs the second pass for one orchestrator. Every retry ends by `until()`; no round starts without
    GAP_MIN_ROUND seconds left, so the bid form and the signing reserve are never touched."""

    def __init__(self, orchestrator):
        self.o = orchestrator
        self.tries: dict[str, int] = {}
        self.reread_done: set[str] = set()
        self.hinted_done: set[str] = set()
        self.round = 0

    def until(self) -> float:
        return self.o.research_deadline - GAP_TAIL

    def left(self) -> float:
        return self.until() - time.monotonic()

    def _sites(self, kind: str) -> list[str]:
        return [n.site for n in getattr(getattr(self.o, "plan", None), "needs", []) if n.kind == kind]

    def feasible(self, gap: Gap) -> bool:
        o = self.o
        if gap.area == "doubt":
            return False  # verify() already sends doubtful mail facts back; a doubt alone is no reason to browse
        if gap.area == "tender":
            return bool(self._sites("tender")) and _module("research_tender") is not None
        if gap.area == "requirement":
            return bool(self._sites("specification"))
        if gap.area in ("erp", "material"):
            return getattr(o, "_erp", None) is not None
        if gap.area == "mail":
            if _module("research_mail") is None or not self._sites("mails"):
                return False
            planned = {k.name for k in getattr(o, "mail_kinds", [])}
            return not planned or gap.key in planned
        return False

    def limit(self, gap: Gap) -> int:
        """Retries a gap gets: a mail kind whose first search finished and read records (its key may simply not
        exist for this tender) gets one, everything else GAP_TRIES."""
        if gap.area == "mail":
            found = (getattr(self.o, "mail_results", None) or {}).get(gap.key)
            if found is not None and found.records and not getattr(found, "stopped", False):
                return 2 if getattr(found, "kept", None) and not gap.reason.startswith(DOUBT) else 1
            if found is not None and getattr(found, "kept", None) and not found.records:
                return GAP_TRIES + 1  # one reread of the kept messages, then the searches
        return GAP_TRIES

    def trace(self, kind: str, **data) -> None:
        self.o.trace.event("gaps", kind, **data)

    async def run(self, first: Calculation, calculate) -> Calculation:
        """Fill gaps in rounds until none is left to retry or the time is up; returns the best calculation. A
        failure of the pass itself never costs the first answer."""
        self.best = first
        try:
            return await self._rounds(first, calculate)
        except Exception as error:
            self.o.board.problem("gaps", f"second pass failed: {type(error).__name__}: {str(error)[:200]}")
            return self.best

    async def _rounds(self, first: Calculation, calculate) -> Calculation:
        best = first
        verifying = getattr(self.o, "verifying", None)  # the verifier, running beside the first answer
        if verifying is not None and not verifying.done() and any(
                self.feasible(g) for g in decisive(detect_gaps(self.o, first.problems), first)):
            # a round will run: its doubted mail kinds may ride along, so their verdicts are worth the wait
            await asyncio.wait({verifying}, timeout=max(0.0, self.left()))
        gaps = decisive(detect_gaps(self.o, first.problems, doubts=getattr(self.o, "gap_rework", False)), first)
        self._report(gaps, set())
        while self.round < GAP_ROUNDS:
            todo = [g for g in gaps if self.feasible(g) and self.tries.get(g.ident, 0) < self.limit(g)]
            if not any(not g.reason.startswith(DOUBT) for g in todo):
                break  # nothing the answer lacks; verifier doubts alone are no reason to browse
            if not todo:
                break
            if self.left() < GAP_MIN_ROUND:
                self.trace("gap_no_time", seconds_left=round(self.left()), open=[g.ident for g in todo])
                break
            self.round += 1
            started = time.monotonic()
            touched = await self.fill(todo)
            if any(g.area == "mail" for g in todo):  # reads that finished before a cut still count
                await self.resolve_mail()
            # Every requirement without a choice is chosen again from the facts as they are now: a "none" made
            # while its stroke reserve was pending reads as "no match" once the reserve is known, and turned a
            # complete bid into a false no_bid in soak run 83438e79.
            touched |= {rid for rid in self.o.requirements if self.o.chosen.get(rid) is None}
            await self.reselect(touched)
            new = calculate()
            after = decisive(detect_gaps(self.o, new.problems), new)
            open_now = {g.ident for g in after}
            filled = [g.ident for g in todo if g.ident not in open_now and not g.reason.startswith(DOUBT)]
            for ident in filled:
                self.trace("gap_filled", gap=ident, round=self.round)
            self._report(after, {g.ident for g in gaps})
            kept = better(new, best)
            self.trace("gap_round", round=self.round, seconds=round(time.monotonic() - started, 1),
                       retried=[g.ident for g in todo], filled=filled, open=sorted(open_now),
                       completeness=list(new.completeness()), kept="new" if kept else "earlier")
            if kept:
                best = self.best = new
            gaps = after
        still = [g.ident for g in gaps if g.area != "doubt" and not g.reason.startswith(DOUBT)]
        if still:
            self.trace("gap_open", gaps=still, rounds=self.round)
        return best

    def _report(self, gaps: list[Gap], known: set[str]) -> None:
        """One 'gap' event per new gap; verifier doubts in one line (there are often many, and none is retried)."""
        for gap in gaps:
            if gap.area != "doubt" and gap.ident not in known:
                self.trace("gap", gap=gap.ident, reason=gap.reason, positions=list(gap.positions))
        doubts = [g.key for g in gaps if g.area == "doubt" and g.ident not in known]
        if doubts:
            self.trace("gap_doubts", facts=doubts[:20], count=len(doubts))

    # ------------------------------------------------------------ one round

    async def fill(self, todo: list[Gap]) -> set[str]:
        """Run the retries side by side until `until()`; returns the requirement ids whose choice may change."""
        o = self.o
        touched: set[str] = set()
        jobs: dict[asyncio.Task, str] = {}

        def job(coro, name: str) -> None:
            jobs[asyncio.create_task(coro, name=f"gap:{name}")] = name

        todo = self.spare_doubts(todo)
        for gap in todo:
            self.tries[gap.ident] = self.tries.get(gap.ident, 0) + 1
            self.trace("gap_retry", gap=gap.ident, round=self.round, attempt=self.tries[gap.ident],
                       seconds_left=round(self.left()))
        areas = {}
        for gap in todo:
            areas.setdefault(gap.area, []).append(gap)
        if any(g.key == "positions" for g in areas.get("tender", [])):
            job(self.tender(), "tender")
        elif "tender" in areas:
            touched |= set(getattr(o, "requirements", {}) or {})  # an amendment can move any requirement
            job(self.tender_qa(), "tender Q&A")
        if "requirement" in areas:
            job(self.specification([g.key for g in areas["requirement"]], touched), "specification")
        for gap in areas.get("erp", []):
            touched.add(gap.key)
            job(self.erp_requirement(gap.key, gap.reason), gap.ident)
        for gap in areas.get("material", []):
            job(self.material(gap.key), gap.ident)
        if "mail" in areas:
            # Kinds that blank or omit fields come first; a kind only the verifier doubted (its doubts are mostly
            # "the quote shows only the header") takes a mail tab only when one is spare: in one sandbox run eight
            # doubted kinds held all four tabs and the one real gap (a setup pool) stayed open.
            kinds = [g.key for g in areas["mail"]]
            if "end_reserves" in kinds:  # a reserve moves the stroke the material must match
                touched |= {rid for rid, req in o.requirements.items() if o.effective(req).pending}
            unread = [k for k in kinds if self.unread(k)]  # kept messages whose reading was cut: read them
            self.reread_done |= set(unread)
            if unread:
                job(self.reread(unread), "mail reread")
            # Kinds whose messages were read but whose facts still leave the gap (soak run a75f0ec3: the reader
            # copied one of three metal index windows, twice): the kept messages are read again, told what the
            # first reading lacked, before the kind is searched once more.
            partial = {g.key: g.reason for g in areas["mail"] if g.key not in unread and self.partial(g)}
            self.hinted_done |= set(partial)
            if partial:
                job(self.reread(list(partial), hints=partial), "mail reread with hint")
            search = [k for k in kinds if k not in unread and k not in partial]
            if search:
                job(self.mails(search), "mails")
        if not jobs:
            return touched
        done, late = await asyncio.wait(jobs, timeout=max(1.0, self.left()))
        for task in late:
            task.cancel()
            o.board.problem("gaps", f"{jobs[task]} retry stopped at the deadline")
        if late:
            await asyncio.gather(*late, return_exceptions=True)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                error = task.exception()
                o.board.problem("gaps", f"{jobs[task]} retry failed: {type(error).__name__}: {str(error)[:160]}")
        return touched

    def spare_doubts(self, todo: list[Gap]) -> list[Gap]:
        """
        Mail kinds that blank or omit fields come first; a kind only the verifier doubted (its doubts are mostly
        "the quote shows only the header") takes a mail tab only when one is spare: in one sandbox run eight
        doubted kinds held all four tabs and the one real gap (a setup pool) stayed open.
        """
        real = [g for g in todo if not (g.area == "mail" and g.reason.startswith(DOUBT))]
        doubted = [g for g in todo if g not in real]
        # doubts alone never start a round: in a complete run that round only delayed the answer (190 s)
        spare = max(0, len(getattr(self.o, "mail_sessions", None) or []) - sum(g.area == "mail" for g in real)) \
            if real else 0
        if doubted[spare:]:
            self.trace("gap_skipped", gaps=[g.ident for g in doubted[spare:]], reason="no spare mail tab")
        return real + doubted[:spare]

    async def reselect(self, rids: set[str]) -> None:
        """The final material choice again for the requirements a retry touched (no browsing)."""
        o = self.o
        rids = {r for r in rids if r in o.requirements}
        if not rids or getattr(o, "erp", None) is None:
            return
        from research import reselect
        timeout = max(10.0, min(75.0, o.research_deadline - time.monotonic()))
        tasks = {rid: asyncio.create_task(reselect([o.effective(o.requirements[rid])], o.erp, o.trace)) for rid in rids}
        _, late = await asyncio.wait(tasks.values(), timeout=timeout)
        for task in late:
            task.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)
        for rid, task in tasks.items():
            if task.done() and not task.cancelled() and task.exception() is None:
                o.chosen.update(task.result())
        self.trace("reselect", chosen={r: o.chosen.get(r) for r in sorted(rids)})

    # ------------------------------------------------------------ the retries

    async def tender(self) -> None:
        for site in self._sites("tender"):
            await self.o.tender(site)
            if getattr(self.o.tender_facts, "positions", None):
                return

    async def tender_qa(self) -> None:
        """The bidder Q&A read once more on fresh tabs; new answers join the earlier reads."""
        recheck = getattr(self.o, "recheck_qa", None)
        if recheck is not None:
            new = await recheck("Read every answer, on every page of the list.", self.until())
            self.trace("qa_reread", new_amendments=new)

    async def specification(self, missing: list[str], touched: set[str]) -> None:
        """A careful spec navigator told which requirement ids are still missing; what it streams goes to the board
        and the ERP like in the first pass (same revision only), and the ERP research for them is awaited."""
        from browser_tools import SiteSession
        from research import research_spec
        o = self.o
        site = self._sites("specification")[0]
        erp = getattr(o, "_erp", None)
        before = set(o.requirements)
        hint = (f"Requirements still missing from what was read so far: {', '.join(missing)}"
                + (f" (revision {o.spec_revision})" if o.spec_revision else "")
                + ". Make sure the pages that hold them are read.")
        context = o.task["brief"].split("## Rules")[0].strip() + "\n\n" + hint
        access = {**o.access[site], "siteId": f"{site}:gap{self.round}"}
        await o.site_up(site)
        session = SiteSession(access, o.contexts[site], o.trace)
        await session.start(until=self.until())
        try:
            result = await research_spec(
                session, o.briefing, context=context, deadline=self.until(), thinking=True,
                max_steps=int(os.environ.get("SPEC_STEPS_FAST", "25")),
                on_requirement=lambda req: o._requirement_found(req, site, session.page.url, session.seen, erp))
            for req in result.requirements:
                o._requirement_found(req, site, "", "", erp)
        finally:
            await _close(session)
        new = [rid for rid in o.requirements if rid not in before]
        touched |= set(new)
        self.trace("spec_retry", found=new, still_missing=[m for m in missing if not any(
            _same_id(m, rid) for rid in o.requirements)])
        tasks = {rid: erp.tasks[rid] for rid in new if erp is not None and rid in erp.tasks}
        if tasks:
            await asyncio.wait(tasks.values(), timeout=max(1.0, self.left()))
            for rid, task in tasks.items():
                if task.done() and not task.cancelled() and task.exception() is None:
                    self.merge(task.result())

    async def erp_requirement(self, rid: str, reason: str) -> None:
        erp = self.o._erp
        _drop_failed(erp)
        options = {}
        if "hint" in inspect.signature(erp.requirement).parameters:  # a hook research.py may offer
            options["hint"] = f"A first search for this requirement gave no usable result ({reason})."
        research = await erp.requirement(self.o.effective(self.o.requirements[rid]), **options)
        self.merge(research)

    async def material(self, number: str) -> None:
        o = self.o
        erp = o._erp
        _drop_failed(erp)
        erp.materials.pop(number, None)
        link = next((c.link for r in (getattr(o.erp, "requirements", None) or []) for c in r.candidates
                     if c.material_number == number and c.link), "")
        record = await erp.material(number, link)
        self._keep_material(number, record)

    def unread(self, kind: str) -> bool:
        """A kind whose search kept messages but whose reading did not finish (cut by the mail deadline): the
        kept page views only need the reader again, no browsing. Once per kind; after that it is searched."""
        found = (getattr(self.o, "mail_results", None) or {}).get(kind)
        return (found is not None and bool(found.kept) and not found.records and kind not in self.reread_done)

    def partial(self, gap: Gap) -> bool:
        """A real mail gap whose kind read records from kept messages, not yet reread with a hint."""
        found = (getattr(self.o, "mail_results", None) or {}).get(gap.key)
        return (found is not None and bool(getattr(found, "kept", None)) and bool(found.records)
                and not gap.reason.startswith(DOUBT) and gap.key not in self.hinted_done)

    async def reread(self, kinds: list[str], hints: dict[str, str] | None = None) -> None:
        """
        Read the kept message views of each kind again; every record read is kept at once (a cut keeps it). With
        `hints`, the reader is told what the facts still lack; a record read later wins over an equal earlier one
        when the mail facts are resolved again.
        """
        read_message = _module("research_mail").read_message
        o = self.o

        def told(kind, name: str):
            if not hints or name not in hints or not dataclasses.is_dataclass(kind):
                return kind
            return dataclasses.replace(kind, what=(
                f"{kind.what}. A first reading of this message left a gap ({hints[name][:200]}): report every "
                f"entry the message lists, with all keys, rates and validity windows, not only the first"))

        async def one(name: str, view: str) -> None:
            found = o.mail_results[name]
            record = await read_message(view, told(found.kind, name), o.reference)
            if record is not None:
                found.records = [*found.records, record]

        await asyncio.gather(*(one(name, view) for name in kinds for view in o.mail_results[name].kept),
                             return_exceptions=True)
        self.trace("mail_reread", kinds={name: len(o.mail_results[name].records) for name in kinds})

    async def resolve_mail(self) -> None:
        resolve = getattr(self.o, "_resolve_mail", None)
        if resolve is None or not getattr(self.o, "mail_sessions", None):
            return
        try:
            await asyncio.wait_for(resolve(), max(5.0, min(20.0, self.o.research_deadline - time.monotonic())))
        except Exception as error:
            self.o.board.problem("gaps", f"mail facts not resolved again: {type(error).__name__}: {str(error)[:120]}")

    async def mails(self, kinds: list[str]) -> None:
        """The missing record kinds on fresh tabs, through the first pass's own mail loop (results add up, all
        kinds are resolved again); without a mail plan or tab, the whole mail research once more."""
        o = self.o
        o.mail_deadline = max(o.mail_deadline, self.until())
        if not o.mail_kinds or not o.mail_sessions:
            await o.mails(self._sites("mails")[0])
            return
        fresh = getattr(_module("research_mail"), "_fresh_tab", None)
        if fresh is not None:
            await asyncio.wait([asyncio.create_task(fresh(s)) for s in o.mail_sessions], timeout=FRESH_TAB_SECONDS)
        await o._research_mail([k for k in o.mail_kinds if k.name in kinds], deadline=self.until())

    # ------------------------------------------------------------ merging (never loses a fact)

    def merge(self, research) -> None:
        """A retried requirement's ERP research replaces the earlier one only when it got further."""
        from research import ErpResearch
        o = self.o
        if research is None:
            return
        if o.erp is None:
            o.erp = ErpResearch()
        old = next((r for r in o.erp.requirements if r.requirement_id == research.requirement_id), None)
        if old is None or research.inspected or not old.inspected:
            o.erp.requirements = [r for r in o.erp.requirements if r is not old] + [research]
            o.chosen[research.requirement_id] = research.selected
        for number, task in list(getattr(o._erp, "materials", {}).items()):
            if task.done() and not task.cancelled() and task.exception() is None:
                self._keep_material(number, task.result())

    def _keep_material(self, number: str, record) -> None:
        o = self.o
        if record is None or o.erp is None:
            return
        old = o.erp.materials.get(number)
        if old is None or len(_incomplete(record)) <= len(_incomplete(old)):
            o.erp.materials[number] = record


def _module(name: str):
    try:
        return __import__(name)
    except ImportError:  # pragma: no cover
        return None


def _drop_failed(erp) -> None:
    """Material reads that failed or were cancelled are read again when a retry asks for them."""
    for number, task in list(erp.materials.items()):
        if task.done() and (task.cancelled() or task.exception() is not None or task.result() is None):
            erp.materials.pop(number, None)


async def _close(session) -> None:
    for page in list(getattr(session, "pages", [])):
        try:
            await page.close()
        except Exception:  # already gone
            continue

