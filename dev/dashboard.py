"""
Run dashboard: every test run and scored run of the team, as the platform reports them, with where the time and the
cost went, joined with our notes from experiments/runs.jsonl and the lessons in docs/learned.md.

Usage (stdlib only, Python 3.9+, from the repo root; needs `atira login`):
    python3 dev/dashboard.py [--open]
Writes docs/dashboard.html (gitignored). Finished runs are cached in experiments/run-cache.json (gitignored), so a
refresh only asks the platform for new runs. Points per field are kept, never the answer values the platform echoes.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import ssl
import statistics
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "experiments" / "runs.jsonl"
CACHE = ROOT / "experiments" / "run-cache.json"
LEARNED = ROOT / "docs" / "learned.md"
OUT = ROOT / "docs" / "dashboard.html"
API = "https://ehl-challenge.atira.ai"
FINAL = ("scored", "submitted", "failed", "timed_out")
FIELDS = ("material_number", "quantity", "base_unit_price", "surcharge_per_unit", "packaging_per_unit",
          "rebate_total", "unit_price", "lead_time_weeks")
# python.org Python on macOS ships without CA certificates; the system bundle works
CA_FILE = os.environ.get("SSL_CERT_FILE") or ("/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None)
TLS = ssl.create_default_context(cafile=CA_FILE)
TOP = ("decision", "tender_reference", "discount_percent", "total_net", "total_consistency", "withdrawn_answer",
       "index_window")


def get(path: str, token: str):
    request = urllib.request.Request(API + path, headers={"x-api-key": token, "User-Agent": "error404ai-dev/1.0"})
    with urllib.request.urlopen(request, timeout=30, context=TLS) as response:
        return json.load(response)


def call_stats(calls: list[dict]) -> dict:
    """Where a run's model time went: waiting for a GPU (queue) against the model answering."""
    if not calls:
        return {}
    queue = [call.get("queue_ms") or 0 for call in calls]
    latency = [call.get("latency_ms") or 0 for call in calls]
    return {
        "queue_s": round(sum(queue) / 1000, 1),
        "model_s": round((sum(latency) - sum(queue)) / 1000, 1),
        "queue_p50_s": round(statistics.median(queue) / 1000, 1),
        "cached_tokens": sum(call.get("cache_read_tokens") or 0 for call in calls),
        "failed": sum(1 for call in calls if call.get("status") != 200),
    }


def field_points(breakdown: dict | None) -> dict:
    """Points per answer field summed over the positions: where the score is lost, without any answer value."""
    if not breakdown:
        return {}
    points = {key: (breakdown.get(key) or {}).get("points") for key in TOP if key in breakdown}
    positions = breakdown.get("positions") or []
    for field in FIELDS:
        points[field] = round(sum((position.get(field) or {}).get("points") or 0 for position in positions), 2)
    points["positions"] = len(positions)
    return points


def load(token: str | None) -> list[dict]:
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    if token:
        for run in get("/v1/runs", token)["runs"]:
            run_id = run["run_id"]
            if run_id in cache and cache[run_id]["status"] in FINAL:
                continue
            score = run.get("score") or {}
            started, finished = run.get("started_at"), run.get("finished_at")
            entry = {
                "run_id": run_id, "kind": run.get("kind"), "status": run.get("status"), "started_at": started,
                "submission_id": run.get("submission_id"), "calls": run.get("calls"),
                "input_tokens": run.get("input_tokens"), "output_tokens": run.get("output_tokens"),
                "cost_usd": (run.get("cost_micro_usd") or 0) / 1e6, "quality": score.get("quality"),
                "eligible": score.get("eligible"), "failure": run.get("failure_class"),
                "files": (run.get("package") or {}).get("files"),
                "seconds": round((score.get("duration_ms") or ((finished or 0) - (started or 0))) / 1000, 1)
                if started and (finished or score.get("duration_ms")) else None,
            }
            if run.get("status") in FINAL:
                try:
                    entry.update(call_stats(get(f"/v1/runs/{run_id}/llm-calls", token).get("calls") or []))
                    detail = get(f"/v1/runs/{run_id}", token).get("score") or {}
                    entry["points"] = field_points(detail.get("breakdown"))
                except OSError as error:
                    print(f"{run_id}: no details ({error})", file=sys.stderr)
            cache[run_id] = entry
        CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    notes = {}
    if RUNS.exists():
        for line in RUNS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                notes[record.get("run_id") or record["id"]] = record
    for run_id, entry in cache.items():
        record = notes.get(run_id) or {}
        label = " · ".join(part for part in (record.get("version"), record.get("experiment")) if part)
        entry.update(label=label, note=record.get("note") or "", commit=record.get("commit") or "")
    return sorted(cache.values(), key=lambda entry: entry.get("started_at") or 0, reverse=True)


def bar(value: float | None, most: float, css: str = "") -> str:
    if not value or not most:
        return ""
    return f'<span class="bar {css}" style="width:{max(2, round(60 * value / most))}px"></span>'


def cells(*values) -> str:
    return "".join(f"<td>{value}</td>" for value in values)


def render(runs: list[dict]) -> str:
    done = [run for run in runs if run.get("quality") is not None]
    best = max(done, key=lambda run: (bool(run["eligible"]), run["quality"],
                                      -max(run["cost_usd"], 0.01) * max(run["seconds"] or 1, 1)), default=None)
    most_s = max((run.get("seconds") or 0 for run in runs), default=0)
    most_cost = max((run.get("cost_usd") or 0 for run in runs), default=0)
    rows = []
    for run in runs:
        seconds, cost = run.get("seconds"), run.get("cost_usd")
        when = dt.datetime.fromtimestamp((run.get("started_at") or 0) / 1000, dt.timezone.utc).strftime("%d %b %H:%M")
        wait = run.get("queue_s")
        product = f"{max(cost, 0.01) * max(seconds, 1):.2f}" if seconds and cost is not None else ""
        per_call = f"{seconds / run['calls']:.1f} s" if seconds and run.get("calls") else ""
        outcome = run.get("failure") or run.get("status") or ""
        rows.append(
            f'<tr class="{"best" if run is best else ""}">'
            + cells(when, "scored" if run.get("kind") == "scored" else "test",
                    html.escape(run.get("label") or ""),
                    "" if run.get("quality") is None else f'<b>{run["quality"]:g}</b>',
                    f'{bar(seconds, most_s)}{seconds / 60:.1f} min' if seconds else "",
                    f'{bar(cost, most_cost, "cost")}${cost:.4f}' if cost else "", product,
                    run.get("calls") or "", per_call,
                    f'{wait:.0f} s ({round(100 * wait / seconds)} %)' if wait and seconds else "",
                    f'{run["model_s"]:.0f} s' if run.get("model_s") else "",
                    f'{(run.get("input_tokens") or 0) / 1000:.0f}k' if run.get("input_tokens") else "",
                    html.escape(outcome),
                    f'<span class="note">{html.escape(run.get("note") or "")}</span>'
                    f'<br><code>{run["run_id"]}</code>')
            + "</tr>")
    points = (best or {}).get("points") or {}
    lost = "".join(
        f"<tr>{cells(html.escape(field), '' if value is None else f'{value:g}')}</tr>"
        for field, value in points.items() if field != "positions")
    lessons = [line[2:].strip() for line in LEARNED.read_text(encoding="utf-8").splitlines()
               if line.startswith("- ")] if LEARNED.exists() else []
    kpis = ""
    if best:
        kpis = "".join(
            f'<div class="kpi"><div>{name}</div><b>{value}</b></div>' for name, value in (
                ("Best quality", f'{best["quality"]:g} / 100'),
                ("Its time", f'{best["seconds"] / 60:.1f} min'),
                ("Its cost", f'${best["cost_usd"]:.4f}'),
                ("Cost × time", f'{max(best["cost_usd"], 0.01) * max(best["seconds"], 1):.2f}'),
                ("Seconds per model call", f'{best["seconds"] / best["calls"]:.1f}' if best.get("calls") else "?"),
                ("Runs tracked", len(runs))))
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>RFQ runs</title>
<style>
:root {{ --bg:#fbfbfa; --fg:#1c1c1a; --dim:#6b6b66; --line:#e3e3df; --time:#4a7fb5; --cost:#c9853a; --hi:#eef5ec; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161615; --fg:#ecece8; --dim:#9a9a94; --line:#30302d; --time:#6fa3d8; --cost:#dba05a; --hi:#1f2a1e; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:1280px; margin:auto; }} h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:28px 0 8px; }}
.dim, .note {{ color:var(--dim); }} .kpis {{ display:flex; flex-wrap:wrap; gap:12px; margin:16px 0; }}
.kpi {{ border:1px solid var(--line); border-radius:8px; padding:10px 14px; min-width:120px; }}
.kpi div {{ color:var(--dim); font-size:12px; }} .kpi b {{ font-size:20px; }}
.scroll {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; }}
th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }}
td:last-child {{ white-space:normal; min-width:260px; }} th {{ color:var(--dim); font-weight:500; font-size:12px; }}
tr.best {{ background:var(--hi); }} code {{ font-size:11px; color:var(--dim); }}
.bar {{ display:inline-block; height:8px; margin-right:6px; border-radius:2px; background:var(--time); }}
.bar.cost {{ background:var(--cost); }} li {{ margin:4px 0; max-width:100ch; }}
</style><main>
<h1>RFQ runs: quality, time and cost</h1>
<div class="dim">Generated {dt.datetime.now(dt.timezone.utc):%d %b %Y %H:%M} UTC from the platform and
experiments/runs.jsonl. Ranking: eligible, then quality, then lower cost × time (USD × seconds). The highlighted row
is our best run. "GPU wait" is time model calls spent queued before the model started answering.</div>
<div class="kpis">{kpis}</div>
<h2>Runs, newest first</h2><div class="scroll"><table>
<tr><th>Start (UTC)</th><th>Kind</th><th>Version</th><th>Quality</th><th>Time</th><th>Cost</th><th>Cost × time</th>
<th>Model calls</th><th>Run time per call</th><th>GPU wait</th><th>Model answering</th><th>Input tokens</th>
<th>Outcome</th><th>Note</th></tr>
{"".join(rows)}</table></div>
<h2>Points per field in the best run ({points.get("positions", 0)} positions)</h2>
<div class="scroll"><table style="width:auto"><tr><th>Field</th><th>Points</th></tr>{lost}</table></div>
<h2>Lessons (docs/learned.md)</h2><ul>{"".join(f"<li>{html.escape(lesson)}</li>" for lesson in lessons)}</ul>
</main></html>"""


def main() -> None:
    try:
        token = subprocess.run(["atira", "token"], capture_output=True, text=True, timeout=30).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        token = None
    if not token:
        print('no atira token (export PATH="$HOME/.local/bin:$PATH"; atira login): using cached runs only')
    try:
        runs = load(token)
    except OSError as error:
        print(f"platform not reachable ({error}): using cached runs only")
        runs = load(None)
    OUT.write_text(render(runs), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(runs)} runs)")
    if "--open" in sys.argv:
        subprocess.run(["open", str(OUT)], check=False)


if __name__ == "__main__":
    main()
