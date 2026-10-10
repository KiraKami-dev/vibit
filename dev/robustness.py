"""
Robustness check: does the agent depend on public-1 specifics (site ids, site names, brief wording, URLs)?

Builds MUTATED runs from the scouting run and calls the agent's own functions on them, read-only:

- site ids renamed to neutral ids (site-a .. site-d, shuffled), the access list shuffled, the brief's site ids and
  product names (the "(Name, `id`)" pairs) replaced consistently;
- the brief's intro and submission paragraphs reworded and reordered by the model (every value kept), its sections
  reordered and the additional-rule bullets shuffled;
- optionally the URLs the briefing step sees masked, so roles must come from the brief's prose alone.

Checks: `briefing.read_brief` maps the bid and mail roles to the renamed ids and extracts the same reference and
bidder as on the unmutated run; `submit.Mailbox.address` finds the bidder address; a short mail lookup (no TAN) finds
a message for the tender; a short tender read lists the positions. The tender site's context blocks every
state-changing request except sign-in/consent, and every request to a submission or TAN endpoint, so the bid form
is never opened and no TAN is ever requested on the shared scouting account.

From the repo root, with `atira models proxy` running (the scouting logins are loaded from `atira scout --env` when
CHALLENGE_SCOUTING is not set):
    HEADLESS=1 uv run --project agent python dev/robustness.py [--runs 1] [--seed N] [--json out.jsonl]
"""
import argparse
import asyncio
import json
import os
import random
import re
import shlex
import string
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import browsers  # noqa: E402
import llm  # noqa: E402
from briefing import Briefing, read_brief  # noqa: E402
from browser_tools import SiteSession, unseen_values  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from submit import Mailbox  # noqa: E402
from tracing import Trace  # noqa: E402
from worker import run_worker  # noqa: E402

PAIR = re.compile(r"\(([^(),`]+),\s*`([\w-]+)`\)")  # "(Product name, `site-id`)" in the brief
NAMES = ["Nordlicht Suite", "Kestrel Exchange", "Orbis Desk", "Quartz Hub", "Atlas Connect", "Fernwood Office"]
SAFE_POST = re.compile(r"login|logout|signin|sign-in|auth|consent|cookie|session", re.IGNORECASE)
FORBIDDEN = re.compile(r"submission|angebotsabgabe|submit|signatur|sign\b|/tan\b|tan-?request|request-?tan",
                       re.IGNORECASE)


def load_env() -> None:
    """Export `atira scout --env` into this process (no eval, nothing printed)."""
    if os.environ.get("CHALLENGE_SCOUTING") and os.environ.get("QWEN_BASE_URL"):
        return
    out = subprocess.run(["atira", "scout", "--env"], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        match = re.match(r"export (\w+)=(.*)", line.strip())
        if match:
            os.environ[match[1]] = shlex.split(match[2])[0] if match[2] else ""


# ---------------------------------------------------------------- mutation


def sections(brief: str) -> list[str]:
    """The brief split at its level-2 headings (the first part is the intro)."""
    return [part for part in re.split(r"(?m)^(?=## )", brief) if part.strip()]


def replace_ids(text: str, ids: dict[str, str]) -> str:
    """Rename site ids where the brief uses them as ids: in backticks and as "- id:" list items."""
    for old, new in ids.items():
        text = text.replace(f"`{old}`", f"`{new}`")
        text = re.sub(rf"(?m)^(\s*-\s*){re.escape(old)}:", rf"\g<1>{new}:", text)
    return text


class Reworded(BaseModel):
    text: str = Field(description="The reworded text")


async def reword(text: str, rng: random.Random) -> tuple[str, str]:
    """The model rewords and reorders `text`, keeping every fact; deterministic sentence shuffle as fallback."""
    agent = Agent(llm.model(), output_type=Reworded, model_settings=llm.settings(thinking=False), retries=2,
                  instructions="You rewrite business texts. Keep every fact, number, date, reference, name, "
                               "website id in backticks, URL and quoted interface label exactly as written.")
    keep = [t.strip(".,:;/-+") for t in re.findall(r"[\w/.:,+-]*\d[\w/.:,+-]*", text)]
    ticks = re.findall(r"`[^`]+`", text)
    prompt = ("Reword the text below with different sentence structure and vocabulary, and reorder its sentences "
              "and paragraphs where the meaning allows. Keep the markdown heading line unchanged and first. Keep the "
              f"language. Do not add or drop information.\n\n{text}")
    for _ in range(3):
        try:
            out = (await agent.run(prompt)).output.text
        except Exception:
            continue
        lost = [k for k in keep if k and k not in out] + [t for t in ticks if t not in out]
        if not lost and out.strip() != text.strip():
            return out.strip() + "\n\n", "model"
    head, _, body = text.partition("\n")
    sentences = re.split(r"(?<=[.!?])\s+", body.strip())
    rng.shuffle(sentences)
    return head + "\n" + " ".join(sentences) + "\n\n", "fallback"


async def mutate(task: dict, rng: random.Random, mask_urls: bool) -> tuple[dict, dict, dict]:
    """A mutated copy of the run for the briefing step, the renaming used and what changed.

    Returns (briefing_task, ids, info); `briefing_task` has masked URLs when `mask_urls`."""
    old_ids = [s["siteId"] for s in task["access"]]
    new_ids = [f"site-{c}" for c in string.ascii_lowercase[:len(old_ids)]]
    rng.shuffle(new_ids)
    ids = dict(zip(old_ids, new_ids))
    brief = task["brief"]
    pairs = dict(PAIR.findall(brief))  # product name -> id
    names = rng.sample(NAMES, len(pairs))
    for (name, _), new_name in zip(pairs.items(), names):
        brief = brief.replace(name, new_name)
    brief = replace_ids(brief, ids)
    parts = sections(brief)
    intro, rest = parts[0], parts[1:]
    how = []
    intro, kind = await reword(intro, rng)
    how.append(f"intro:{kind}")
    for i, part in enumerate(rest):
        heading = part.split("\n", 1)[0].lower()
        if "submission" in heading or "abgabe" in heading:
            rest[i], kind = await reword(part, rng)
            how.append(f"submission:{kind}")
        elif part.count("\n- ") >= 3 and "access" not in heading:
            head, _, body = part.partition("\n")
            bullets = re.split(r"(?m)^(?=- )", body.strip())
            lead = [b for b in bullets if not b.startswith("- ")]
            items = [b for b in bullets if b.startswith("- ")]
            rng.shuffle(items)
            rest[i] = head + "\n" + "".join(lead) + "".join(b.rstrip() + "\n" for b in items) + "\n"
    rng.shuffle(rest)
    brief = intro + "".join(rest)
    access = [{**s, "siteId": ids[s["siteId"]]} for s in task["access"]]
    rng.shuffle(access)
    submission = dict(task.get("submission", {}))
    if submission.get("siteId") in ids:
        submission["siteId"] = ids[submission["siteId"]]
    mutated = {**task, "brief": brief, "access": access, "submission": submission}
    if mask_urls:
        hosts = {s["url"].rstrip("/"): f"https://{''.join(rng.choices(string.ascii_lowercase, k=8))}.example.net"
                 for s in task["access"]}
        for real, fake in hosts.items():
            brief = brief.replace(real, fake)
        submission = {**submission, "url": hosts.get(submission.get("url", "").rstrip("/"), submission.get("url"))}
        access = [{**s, "url": hosts[s["url"].rstrip("/")]} for s in access]
        mutated = {**mutated, "brief": brief, "access": access, "submission": submission}
    leftover = [old for old in old_ids if f"`{old}`" in mutated["brief"] or re.search(
        rf"(?m)^\s*-\s*{re.escape(old)}:", mutated["brief"])]
    return mutated, ids, {"ids": ids, "reword": how, "masked_urls": mask_urls, "leftover_ids": leftover,
                          "renamed_products": len(pairs)}


# ---------------------------------------------------------------- read-only guard


def guard(context, blocked: list[str], label: str) -> None:
    """Abort state-changing requests (except sign-in/consent) and anything that looks like submission or TAN."""
    async def handle(route):
        request = route.request
        url = request.url
        if FORBIDDEN.search(url) or (request.method not in ("GET", "HEAD") and not SAFE_POST.search(url)):
            blocked.append(f"{label} {request.method} {url[:120]}")
            await route.abort()
        else:
            await route.continue_()
    return context.route("**/*", handle)


# ---------------------------------------------------------------- checks


class Position(BaseModel):
    number: str = Field(description="The position number as shown")
    description: str
    quantity: str = Field(description="The quantity as shown")
    unit: str


class TenderPositions(BaseModel):
    tender_reference: str
    positions: list[Position]


class TenderMessage(BaseModel):
    subject: str
    sender: str = Field(description="The sender's email address as shown")
    received: str = Field(description="When the message arrived, as shown")
    reference: str = Field(description="The tender reference the message is about, as written in it")


def same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


async def timed(coro):
    started = time.monotonic()
    try:
        value = await coro
        return value, round(time.monotonic() - started, 1), None
    except Exception as error:  # a crash is a failed check, not a failed script
        return None, round(time.monotonic() - started, 1), f"{type(error).__name__}: {str(error)[:200]}"


async def one_run(task: dict, seed: int, trace: Trace) -> list[dict]:
    rng = random.Random(seed)
    results: list[dict] = []

    def record(check: str, ok: bool, seconds: float, detail: str) -> None:
        results.append({"seed": seed, "check": check, "ok": bool(ok), "seconds": seconds, "detail": detail})
        print(f"  {'PASS' if ok else 'FAIL'}  {check:<28} {seconds:6.1f}s  {detail}", flush=True)

    started = time.monotonic()
    plain_ids, ids, info = await mutate(task, rng, mask_urls=False)
    masked, _, info_masked = await mutate(task, random.Random(seed), mask_urls=True)  # same renaming, masked URLs
    record("mutation", not info["leftover_ids"] and not info_masked["leftover_ids"],
           round(time.monotonic() - started, 1),
           f"ids {info['ids']}, products renamed {info['renamed_products']}, reword {info['reword']}")
    if os.environ.get("ROBUSTNESS_DUMP"):
        Path(os.environ["ROBUSTNESS_DUMP"]).write_text(masked["brief"], encoding="utf-8")
    # the expected roles come from the unmutated run's briefing, mapped through the renaming
    (base, t_base, e_base), (mut, t_mut, e_mut), (msk, t_msk, e_msk) = await asyncio.gather(
        timed(read_brief(task)), timed(read_brief(plain_ids)), timed(read_brief(masked)))
    stated = task.get("submission", {}).get("siteId")  # the run contract names the bid site, when it does
    record("briefing original", base is not None and (not stated or base.bid_site == stated), t_base,
           e_base or f"bid={base.bid_site} mail={base.mail_site} (submission.siteId {stated})")
    if base is None:
        return results
    roles = {"bid": ids[base.bid_site], "mail": ids[base.mail_site]}
    for name, out, seconds, error in (("briefing renamed ids", mut, t_mut, e_mut),
                                      ("briefing masked urls", msk, t_msk, e_msk)):
        if out is None:
            record(name, False, seconds, error or "no output")
            continue
        wrong = [f for f in ("tender_reference", "company_name", "vat_id")
                 if not same(getattr(out, f), getattr(base, f))]
        ok = out.bid_site == roles["bid"] and out.mail_site == roles["mail"] and not wrong
        record(name, ok, seconds, f"bid={out.bid_site} (want {roles['bid']}) mail={out.mail_site} "
                                  f"(want {roles['mail']}) differs={wrong} steps={len(out.submission_steps)} chars")
    briefing: Briefing = mut or base
    sites = {s["siteId"]: s for s in plain_ids["access"]}  # renamed ids, real URLs
    if briefing.bid_site not in sites or briefing.mail_site not in sites:
        return results

    deadline = time.monotonic() + float(os.environ.get("ROBUSTNESS_SECONDS", "420"))
    blocked: list[str] = []
    async with async_playwright() as playwright:
        bid_ctx, mail_ctx = await browsers.contexts(playwright, 2)
        await guard(bid_ctx, blocked, "tender")
        bid = SiteSession(sites[briefing.bid_site], bid_ctx, trace)
        mail = SiteSession(sites[briefing.mail_site], mail_ctx, trace)
        # agent.solve() starts the sessions without a time limit; here a hung start fails the check instead
        (_, t_start, e_start) = await timed(asyncio.wait_for(asyncio.gather(bid.start(), mail.start()), 120))
        record("sessions start", e_start is None, t_start, e_start or f"{bid.name}, {mail.name} signed in")
        if e_start:
            return results
        mailbox = Mailbox(mail, briefing, deadline)
        rules = "".join(part for part in sections(plain_ids["brief"])
                        if "access" not in part.split("\n", 1)[0].lower())  # never the logins

        async def mail_checks():
            address, t_addr, e_addr = await timed(mailbox.address())
            ok = bool(address and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address) and address in mail.seen)
            record("mail address", ok, t_addr, e_addr or f"{'found' if address else 'empty'}, "
                                               f"grounded={bool(address) and address in mail.seen}")
            goal = (f"Find the sales director's discount approval for exactly tender {briefing.tender_reference} "
                    "(the brief's rules in the context say who sends it and how the search works), open the most "
                    "recent one and return its subject, sender, arrival time and the reference it names. Read only: "
                    "do not send, move or delete anything.")
            async with mailbox.lock:
                found, t_msg, e_msg = await timed(run_worker(mail, goal, TenderMessage, context=rules,
                                                             deadline=deadline, max_steps=20, notes=False))
            message = found[0] if found else None
            domain = address.rsplit("@", 1)[-1].casefold() if address else "?"
            ok = bool(message and same(message.reference, briefing.tender_reference)
                      and message.sender.strip().casefold().endswith("@" + domain))
            record("mail lookup (no TAN)", ok, t_msg, e_msg or (
                f"reference match={same(message.reference, briefing.tender_reference)}, sender in bidder domain="
                f"{message.sender.strip().casefold().endswith('@' + domain)}" if message else "no result"))

        async def tender_check():
            goal = (f"Open tender {briefing.tender_reference} and list every position of its bill of quantities "
                    "(number, short description, quantity, unit) with the original quantities the tender "
                    "documents state (ignore later clarifications). Read only: do not "
                    "open the bid submission, register interest or request anything; reading the tender's pages "
                    "and documents is enough.")
            found, seconds, error = await timed(run_worker(bid, goal, TenderPositions, deadline=deadline,
                                                           max_steps=30, thinking=True, notes=False))
            out = found[0] if found else None
            numbers = [p.number for p in out.positions] if out else []
            ok = bool(out and numbers and len(set(numbers)) == len(numbers)
                      and same(out.tender_reference, briefing.tender_reference)
                      and not unseen_values(" ".join(p.quantity for p in out.positions), bid.seen))
            record("tender positions", ok, seconds, error or (
                f"{len(numbers)} positions {numbers}" if out else "no result"))

        await asyncio.gather(mail_checks(), tender_check())
    record("read-only guard", True, 0.0, f"{len(blocked)} requests blocked" + (f": {blocked[:3]}" if blocked else ""))
    return results


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--json", default=None, help="append one JSON line per check to this file")
    args = parser.parse_args()
    load_env()
    task = json.loads(os.environ["CHALLENGE_SCOUTING"])
    trace = Trace(os.environ.get("TRACE_DIR", str(Path(__file__).resolve().parent.parent / "agent" / "traces")))
    all_results = []
    for i in range(args.runs):
        seed = args.seed + i if args.seed is not None else random.randrange(10**6)
        print(f"\n== robustness run {i + 1}/{args.runs} (seed {seed})", flush=True)
        started = time.monotonic()
        results = await one_run(task, seed, trace)
        print(f"  total {time.monotonic() - started:.0f}s, {sum(r['ok'] for r in results)}/{len(results)} passed",
              flush=True)
        all_results += results
    if args.json:
        with open(args.json, "a", encoding="utf-8") as out:
            for r in all_results:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\ntrace: {trace.path}")


if __name__ == "__main__":
    asyncio.run(main())
