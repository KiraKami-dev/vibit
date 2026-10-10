"""
Clef vs Qwen on hand-labelled decisions from real page views (dev/clef_eval/data/items.json, local and gitignored).

Decision types: `choice` (which link or control serves a goal), `noul_doc` (right document, revision, record, date),
`noul_outcome` (is the page an error, empty, locked or decoy page after an action), `prefilter` (which rows of a list
matter, one decision per row). Each model sees the same page text and question.

    uv run --project agent python dev/clef_eval/eval.py [--reps 2] [--only clef,qwen-off]

Needs `atira models proxy` (defaults to its local URLs). Prints a table and writes data/results.json.
"""
import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "agent"))
os.environ.setdefault("CLEF_BASE_URL", "http://127.0.0.1:8765/clef")
os.environ.setdefault("CLEF_API_KEY", "local")
os.environ.setdefault("QWEN_BASE_URL", "http://127.0.0.1:8765/qwen/v1")
os.environ.setdefault("QWEN_API_KEY", "local")

import clef
from openai import AsyncOpenAI

PRICE = {"clef_in": 0.24, "qwen_in": 0.15, "qwen_cached": 0.016, "qwen_out": 0.47}  # $ per M tokens
qwen = AsyncOpenAI(base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ["QWEN_API_KEY"], timeout=120,
                   max_retries=3)
usage = {"clef_in": 0}


OUTCOME = clef.OUTCOME_QUESTION


def doc_state(it):
    return {"context": it["scenario"], **({"record": it["state"]} if "state" in it else {"page": it["page"]})}


def options(it):
    return clef.options_from_view(it["page"])


# ---- Clef

async def clef_item(it, variant="batch"):
    t = it["type"]
    if t == "choice":
        r = await clef.choose(it["page"], f"{it['goal']} ({it['scenario']})", options(it))
        return [r[0] if r else None]
    if t == "noul_doc":
        p = await clef.yes(doc_state(it), it["q"])
        return [None if p is None else p >= 0.5]
    if t == "noul_outcome":
        p = await clef.dead_end(it["page"], it["action"])  # the agent's own check
        if p is not None:
            print(f"  outcome p={p:.3f} label={it['ans']} view={it['view']}", flush=True)
        return [None if p is None else p >= 0.5]
    if t == "prefilter":
        if variant == "row":
            ps = await asyncio.gather(*[clef.yes({"context": it["scenario"], "row": row},
                                                 f"Does this row: {it['criterion']}?") for row in it["rows"]])
        else:
            state = {"context": it["scenario"], "rows": {str(i + 1): r for i, r in enumerate(it["rows"])}}
            ps = await clef.yes_each(state, [f"Does row {i + 1}: {it['criterion']}?" for i in range(len(it["rows"]))])
        return [None if p is None else p >= 0.5 for p in (ps or [None] * len(it["rows"]))]


# ---- Qwen

async def ask(prompt: str, thinking: bool) -> str:
    reply = await qwen.chat.completions.create(
        model="qwen3.8-flash-next", messages=[{"role": "user", "content": prompt}], temperature=0.2,
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
    u = reply.usage
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    usage["qwen_in"] = usage.get("qwen_in", 0) + u.prompt_tokens - cached
    usage["qwen_cached"] = usage.get("qwen_cached", 0) + cached
    usage["qwen_out"] = usage.get("qwen_out", 0) + u.completion_tokens
    return reply.choices[0].message.content or ""


def yesno(text: str):
    m = re.search(r"\b(yes|no|ja|nein)\b", text.lower())
    return None if not m else m.group(1) in ("yes", "ja")


async def qwen_item(it, thinking):
    t = it["type"]
    head = f"You decide one step for a web agent. {it['scenario']}\n\n"
    if t == "choice":
        opts = "\n".join(f"{k}: {v}" for k, v in options(it).items())
        text = await ask(f"{head}Goal: {it['goal']}\n\nPage:\n{it['page']}\n\nOptions:\n{opts}\n\nWhich element "
                         f"should be used next to reach the goal? Answer with its key only (e.g. e3).", thinking)
        m = re.search(r"\be\d+\b", text)
        return [m.group(0) if m else None]
    if t == "noul_doc":
        body = f"Record:\n{it['state']}" if "state" in it else f"Page:\n{it['page']}"
        return [yesno(await ask(f"{head}{body}\n\nQuestion: {it['q']} Answer yes or no only.", thinking))]
    if t == "noul_outcome":
        return [yesno(await ask(f"{head}Page:\n{it['page']}\n\nQuestion: {OUTCOME.format(action=it['action'])} "
                                f"Answer yes or no only.", thinking))]
    if t == "prefilter":
        rows = "\n\n".join(f"Row {i + 1}:\n{r}" for i, r in enumerate(it["rows"]))
        text = await ask(f"{head}{rows}\n\nWhich rows {it['criterion']}? Answer with a JSON list of row numbers "
                         f"only, e.g. [1, 3], or [] for none.", thinking)
        m = re.search(r"\[[\d,\s]*\]", text)
        picked = set(json.loads(m.group(0))) if m else None
        return [None if picked is None else (i + 1) in picked for i in range(len(it["rows"]))]


METHODS = {
    "clef": lambda it: clef_item(it, "batch"),
    "clef-row": lambda it: clef_item(it, "row"),
    "qwen-off": lambda it: qwen_item(it, False),
    "qwen-on": lambda it: qwen_item(it, True),
}


def correct(it, answers):
    if it["type"] == "choice":
        return [answers[0] in it["ok"]]
    if it["type"] == "prefilter":
        return [a == label for a, label in zip(answers, it["labels"])]
    return [answers[0] == it["ans"]]


async def run(method, items, reps, parallel=4):
    gate = asyncio.Semaphore(parallel)
    records = []

    async def one(i, it, rep):
        async with gate:
            started = time.monotonic()
            try:
                answers = await METHODS[method](it)
            except Exception as error:
                print(f"  {method} item {i} failed: {error}", flush=True)
                answers = [None] * (len(it["rows"]) if it["type"] == "prefilter" else 1)
            seconds = time.monotonic() - started
            records.append({"method": method, "item": i, "rep": rep, "type": it["type"], "seconds": seconds,
                            "answers": answers, "correct": correct(it, answers)})

    await asyncio.gather(*[one(i, it, rep) for rep in range(reps) for i, it in enumerate(items)])
    return records


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--only", default="clef,clef-row,qwen-off,qwen-on")
    parser.add_argument("--types", default="", help="comma-separated decision types to keep (default: all)")
    args = parser.parse_args()
    items = json.loads((HERE / "data" / "items.json").read_text())
    if args.types:
        items = [it for it in items if it["type"] in args.types.split(",")]
    results = {}
    for method in args.only.split(","):
        before = {**usage, "clef_in": clef.USAGE["input_tokens"]}
        recs = await run(method, items, args.reps)
        usage["clef_in"] = clef.USAGE["input_tokens"]
        spent = {k: usage.get(k, 0) - before.get(k, 0) for k in usage}
        results[method] = {"records": recs, "usage": spent}
        print(f"{method}: done", flush=True)
    (HERE / "data" / "results.json").write_text(json.dumps(results, indent=1, default=str))
    table(results, items, args.reps)


def table(results, items, reps):
    types = ["choice", "noul_doc", "noul_outcome", "prefilter"]
    print(f"\n{'type':<14}{'method':<10}{'acc':>12}{'med s':>8}{'p90 s':>8}")
    for t in types:
        for method, res in results.items():
            recs = [r for r in res["records"] if r["type"] == t]
            if not recs:
                continue
            ok = sum(sum(r["correct"]) for r in recs)
            n = sum(len(r["correct"]) for r in recs)
            secs = sorted(r["seconds"] for r in recs)
            p90 = secs[min(len(secs) - 1, int(0.9 * len(secs)))]
            print(f"{t:<14}{method:<10}{f'{ok}/{n} {100 * ok / n:.0f}%':>12}{statistics.median(secs):>8.2f}"
                  f"{p90:>8.2f}")
    print("\nmisses (method: item numbers, x repeats):")
    for method, res in results.items():
        misses = {}
        for r in res["records"]:
            if not all(r["correct"]):
                misses[r["item"]] = misses.get(r["item"], 0) + 1
        print(f"  {method:<10} " + ", ".join(f"{i}x{n}" for i, n in sorted(misses.items())))
    print("\ncost per decision call (simulated $):")
    for method, res in results.items():
        u = res["usage"]
        calls = len(res["records"])
        if method.startswith("clef"):
            cost = u.get("clef_in", 0) * PRICE["clef_in"] / 1e6
        else:
            cost = (u.get("qwen_in", 0) * PRICE["qwen_in"] + u.get("qwen_cached", 0) * PRICE["qwen_cached"]
                    + u.get("qwen_out", 0) * PRICE["qwen_out"]) / 1e6
        print(f"  {method:<10} ${cost / calls:.6f} per item ({calls} items, usage {u})")


if __name__ == "__main__":
    asyncio.run(main())
