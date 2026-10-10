"""
Quick decisions with Cloudflare Clef, the hosted decision model, beside Qwen.

Clef answers fixed-form questions about a state in about a second: `choice` picks one of our options, `noul` gives the
probability of yes. Off unless AGENT_CLEF=1 (or USE_CLEF=1), so runs with and without it compare side by side; ship
it on only with "clef" in agent.json "models". Every helper returns None when Clef fails, and the caller goes on
without it.

Measured on hand-labelled page views (dev/clef_eval/RESULTS.md): Clef loses to Qwen without thinking at picking a
link and at judging whether a record is the right one (ordering, look-alike references, numbers), but it is exact and
fast at "is this an error, empty, locked or decoy page after my action", where Qwen without thinking slips. So the
only agent use is that check: the `page_check` tool, and `outcome_hint` for a hook after actions.
"""
import asyncio
import os
import re
import time

import httpx2 as httpx
from pydantic_ai import RunContext

TIMEOUT = float(os.environ.get("CLEF_TIMEOUT", "20"))
MAX_STATE = 12000  # characters of page text sent; Clef reads text far shorter than Qwen's context
_limit = asyncio.Semaphore(int(os.environ.get("CLEF_CONCURRENCY", "12")))  # team limit is 16 at once
USAGE = {"calls": 0, "input_tokens": 0, "seconds": 0.0}  # this process's Clef use, for the trace and cost
ELEMENT = re.compile(r"\[(e\d+) (link|button|summary|tab|menuitem)\b([^\]]*)\]")


def enabled() -> bool:
    on = os.environ.get("AGENT_CLEF", os.environ.get("USE_CLEF", "0")) == "1"
    return on and bool(os.environ.get("CLEF_BASE_URL"))


async def decide(state, questions: dict, images: list[str] | None = None, retries: int = 2) -> dict | None:
    """One /v1/decide call: the raw answers by question key, or None after `retries` failed attempts."""
    if not os.environ.get("CLEF_BASE_URL"):
        return None
    body = {"state": state, "questions": questions}
    if images:
        body["images"] = images[:4]
    url = os.environ["CLEF_BASE_URL"].rstrip("/") + "/v1/decide"
    headers = {"User-Agent": "error404ai-rfq-agent"}
    if os.environ.get("CLEF_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['CLEF_API_KEY']}"
    for attempt in range(retries + 1):
        started = time.monotonic()
        try:
            async with _limit, httpx.AsyncClient(timeout=TIMEOUT) as client:
                response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
            tokens = data.get("usage", {}).get("input_tokens", 0)
            USAGE["calls"] += 1
            USAGE["input_tokens"] += tokens
            USAGE["seconds"] += time.monotonic() - started
            print(f"[clef] {response.status_code} {time.monotonic() - started:.1f}s {tokens} tokens", flush=True)
            return data["answers"]
        except Exception as error:
            print(f"[clef] failed ({type(error).__name__}: {str(error)[:120]})", flush=True)
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    return None


def options_from_view(view: str) -> dict[str, str]:
    """The links and buttons of a page view by ref, each with the words of its row, for a `choice` question."""
    options: dict[str, str] = {}
    lines = view.splitlines()
    for i, line in enumerate(lines):
        matches = list(ELEMENT.finditer(line))
        if not matches:
            continue
        row = ELEMENT.sub(lambda m: m.group(3).strip(), line).strip()
        if len(ELEMENT.sub("", line).strip(" |-")) < 3 and i:  # a row of bare controls: name what precedes it
            row = f"{lines[i - 1].strip()[:120]} / {row}"
        for m in matches:
            options[m.group(1)] = f"{m.group(2)} {m.group(3).strip()}  (row: {row[:240]})"
    return options


async def choose(view: str, goal: str, options: dict[str, str] | None = None) -> tuple[str, float] | None:
    """Which option (by key) best serves `goal` on this view, and Clef's probability for it."""
    options = options or options_from_view(view)
    if not options:
        return None
    answers = await decide({"goal": goal, "page": view[:MAX_STATE]},
                           {"pick": {"type": "choice", "instructions": f"Which element should be used next to reach "
                                     f"the goal: {goal}", "criteria": options}})
    if not answers:
        return None
    pick = answers["pick"]
    return pick["choice"], pick.get("probabilities", {}).get(pick["choice"], pick.get("confidence", 0.0))


async def yes(state, question: str) -> float | None:
    """Clef's probability that the answer to a yes/no question about `state` is yes."""
    answers = await decide(state, {"q": {"type": "noul", "instructions": question}})
    return None if answers is None else answers["q"]["noul"]


async def yes_each(state, questions: list[str]) -> list[float] | None:
    """Several yes/no questions about one state in one call (e.g. one per list row), in order."""
    answers = await decide(state, {f"q{i}": {"type": "noul", "instructions": q} for i, q in enumerate(questions)})
    return None if answers is None else [answers[f"q{i}"]["noul"] for i in range(len(questions))]


OUTCOME_QUESTION = ("Is this page an error, an empty result, a locked or expired state, a dead end or a decoy, rather "
                    "than real content that moves the task on? Last action: {action}")


async def dead_end(view: str, action: str) -> float | None:
    """Clef's probability that `view`, seen after `action`, is an error, empty, locked or decoy page."""
    return await yes({"last_action": action, "page": view[:MAX_STATE]}, OUTCOME_QUESTION.format(action=action))


async def outcome_hint(view: str, action: str) -> str:
    """A line to put above a view when Clef finds it a dead end after `action` (threshold 0.5), else ""."""
    p = await dead_end(view, action) if enabled() else None
    return "" if p is None or p < 0.5 else (f"Check: this looks like an error, empty, locked or decoy page after "
                                             f"{action} (p={p:.2f}); go back or try another way.")


async def page_check(ctx: RunContext, last_action: str) -> str:
    """Ask a fast second opinion whether the current page is an error, empty, locked or decoy page after an action.

    Use it when unsure whether your last action worked, before relying on the page. `last_action` says what you
    did and expected, e.g. "searched the archive for the tender reference".
    """
    session = ctx.deps.session
    p = await dead_end(session.view, last_action)
    session.trace.event(session.name, "clef", check="outcome", action=last_action[:120], p_error=p)
    if p is None:
        return "No second opinion available; judge the page yourself."
    if p >= 0.5:
        return f"Likely an error, empty, locked or decoy page (p={p:.2f}): go back or try another way."
    return f"Likely real content (p_error={p:.2f}); the action seems to have worked."


def tools() -> tuple:
    """Extra worker tools when Clef is on; pass to run_worker(tools=...)."""
    return (page_check,) if enabled() else ()
