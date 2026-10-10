"""
The findings board: typed facts the research workers post, each with its source, and events so that a worker
waiting for another's facts starts the moment they exist.

A fact is a (kind, key) pair with a JSON-able value and a `Source` (site, page, quote). Workers post facts; the
orchestrator and the verifier read them; `wait(kind)` blocks until a producer has finished posting that kind
(or the timeout passes), so the pipeline needs no fixed order in code.

The protocols at the end state what the orchestrator expects from the tender Q&A and mail research modules
(built separately); the orchestrator adapts to them through `getattr` where their shapes differ.
"""
import asyncio
import time
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Source(BaseModel):
    site: str = Field(description="siteId the fact was read on")
    url: str = Field(default="", description="The page it was read from, if known")
    quote: str = Field(default="", description="The page text the value was copied from, as shown")


class Fact(BaseModel):
    kind: str
    key: str
    value: Any
    source: Source
    worker: str = ""
    posted_at: float = 0.0
    verdict: str = Field(default="", description="'' until verified; 'ok' or the verifier's doubt")

    @property
    def ident(self) -> str:
        return f"{self.kind}/{self.key}"


class Board:
    def __init__(self, trace=None):
        self.facts: dict[tuple[str, str], Fact] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._first: dict[str, asyncio.Event] = {}
        self.started = time.monotonic()
        self.trace = trace
        self.problems: list[str] = []

    def _event(self, table: dict[str, asyncio.Event], kind: str) -> asyncio.Event:
        if kind not in table:
            table[kind] = asyncio.Event()
        return table[kind]

    def post(self, kind: str, key: str, value: Any, source: Source, worker: str = "") -> Fact:
        fact = Fact(kind=kind, key=key, value=value, source=source, worker=worker,
                    posted_at=round(time.monotonic() - self.started, 1))
        self.facts[(kind, key)] = fact
        self._event(self._first, kind).set()
        if self.trace:
            self.trace.event("board", "fact", quiet=True, fact_kind=kind, key=key, by=worker,
                             value=value if isinstance(value, (str, int, float)) else str(value)[:300])
        return fact

    def problem(self, worker: str, text: str) -> None:
        self.problems.append(f"{worker}: {text}")
        if self.trace:
            self.trace.event(worker, "problem", text=text[:300])

    def finish(self, kind: str) -> None:
        """A producer has posted everything it will for this kind (possibly nothing)."""
        self._event(self._done, kind).set()
        self._event(self._first, kind).set()

    def finished(self, kind: str) -> bool:
        return kind in self._done and self._done[kind].is_set()

    def get(self, kind: str) -> dict[str, Fact]:
        return {key: fact for (k, key), fact in self.facts.items() if k == kind}

    def values(self, kind: str) -> dict[str, Any]:
        return {key: fact.value for key, fact in self.get(kind).items()}

    async def wait(self, kind: str, timeout: float | None = None, first: bool = False) -> dict[str, Fact]:
        """The facts of a kind once its producer finished (or the first one, with `first`); what exists on timeout."""
        event = self._event(self._first if first else self._done, kind)
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            if self.trace:
                self.trace.event("board", "wait_timeout", fact_kind=kind, seconds=timeout)
        return self.get(kind)

    def summary(self) -> str:
        lines = []
        for (kind, key), fact in sorted(self.facts.items()):
            flag = "" if not fact.verdict else (" [ok]" if fact.verdict == "ok" else f" [doubt: {fact.verdict}]")
            lines.append(f"- {kind}/{key}: {str(fact.value)[:160]} ({fact.source.site}){flag}")
        return "\n".join(lines)


# ---------------------------------------------------------------- what the orchestrator expects from the other
# research modules (tender Q&A, mail). Attributes are read with getattr and defaults, so a module that lacks one
# of them still works.


@runtime_checkable
class PositionFactsLike(Protocol):
    position: str
    requirement: str
    quantity: int
    unit: Any  # calc.Unit or its text
    setup_fee: Any


@runtime_checkable
class TenderFactsLike(Protocol):
    """`research_tender.research_tender(session, reference, scenario, *, deadline)` returns this (or None)."""
    positions: list  # PositionFactsLike, plus optional condition_group, packaging_key, commitment_key,
    #                  setup_pool_key, bonus_key
    amendments: list  # calc.Amendment
    withdrawn: list


@runtime_checkable
class MailKindLike(Protocol):
    """`research_mail.plan_searches(brief, sites, findings)` returns a list of these (frozen dataclasses);
    `sites` maps each siteId of the run to its URL, `site` is the siteId the model placed the record on."""
    name: str
    site: str


@runtime_checkable
class MailFactsLike(Protocol):
    """`research_mail.resolve_mail(results, reference, domain, scenario, mailbox)` returns this; the orchestrator
    runs `research_kind(session, kind, reference, *, deadline)` per kind over its tabs itself (own deadline,
    partial results kept) and finds the address with `mailbox_address(session)`."""
    mailbox: str
    approval: Any  # calc.Approval | None
    end_reserves: dict  # key -> object with reserve_per_end_mm (and revision)
    fx_rates: dict
    metal_indices: dict


@runtime_checkable
class BidClausesLike(Protocol):
    """`research_mail.bid_clauses(tender, mail)`: the calc.BidInput parts the Q&A and the mails decide."""
    approval: Any
    fx_rates: dict
    metal_indices: dict
    setup_pool: Any
    project_bonus: Any
    positions: list  # per position: position, quantity, unit, discount_scope, setup_fee, packaging, commitment
    problems: list
