"""
Experiment log: one JSON line per run (test runs and submissions in experiments/runs.jsonl, committed; local runs in
experiments/local-runs.jsonl, gitignored), rendered to docs/experiments.md (gitignored). Lessons: docs/learned.md.

Usage (stdlib only, Python 3.9+, from the repo root):
    python3 dev/track.py local [--trace PATH] [--version v1] [--experiment NAME] [--note "..."]
        Summarise a local run from its trace (default: newest run-*.jsonl in $TRACE_DIR, else agent/traces/).
    python3 dev/track.py platform <run_|reh_|sub_ id> [--version v1] [--experiment NAME] [--note "..."]
        Record a test run or scored submission as `atira status <id> --json` reports it (needs `atira login`).
    python3 dev/track.py report
        Rewrite docs/experiments.md (gitignored) from both logs.

Common options: --commit SHA [--dirty yes|no] records the code as it was when the run started (the wrappers
dev/run-local.sh and dev/test-run.sh pass both), --force records an id again (the report shows the newest record per
id). Commit experiments/runs.jsonl after recording a test run or submission.
Never put credentials or copied site content into notes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "experiments" / "runs.jsonl"  # test runs and submissions: committed, merged with merge=union
LOCAL_RUNS = ROOT / "experiments" / "local-runs.jsonl"  # local runs: frequent and unscored, gitignored
DOC = ROOT / "docs" / "experiments.md"  # generated, gitignored; lessons live in docs/learned.md
DOC_HEADER = """# Experiments (generated, not committed)

Test runs and submissions from `experiments/runs.jsonl` (committed) plus this machine's local runs from
`experiments/local-runs.jsonl` (gitignored), newest first. Regenerate with `python3 dev/track.py report`. Lessons go
in `docs/learned.md`, by hand.

Commit is the repo HEAD when the run started (`*` = uncommitted changes under `agent/`; `atira package <id>` shows the
exact upload). Tokens are model input/output tokens summed over workers (local) or as the platform reports them.
Quality is the platform score (0 to 100); local runs are not scored.
"""
UTC = dt.timezone.utc  # noqa: UP017 (dt.UTC needs Python 3.11; macOS python3 is 3.9)


def utc_iso(when: dt.datetime) -> str:
    return when.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def from_ms(ms: int | None) -> str | None:
    return utc_iso(dt.datetime.fromtimestamp(ms / 1000, UTC)) if ms else None


def git_info(commit: str | None, dirty: str | None) -> dict:
    """The commit and whether agent/ had uncommitted changes; the wrappers pass both as they were at the start."""
    if commit:
        return {"commit": commit, "dirty": {"yes": True, "no": False}.get(dirty or "")}

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()

    try:
        return {
            "commit": git("rev-parse", "--short", "HEAD"),
            "dirty": bool(git("status", "--porcelain", "--", "agent")),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"commit": None, "dirty": None}


def agent_config() -> dict:
    try:
        config = json.loads((ROOT / "agent" / "agent.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: config[key] for key in ("models", "browsers") if key in config}


def load_runs(*paths: Path) -> list[dict]:
    runs = []
    for path in paths or (RUNS, LOCAL_RUNS):
        if path.exists():
            runs += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return runs


def append_run(record: dict, force: bool) -> None:
    path = LOCAL_RUNS if record["kind"] == "local" else RUNS
    if not force and any(run["id"] == record["id"] for run in load_runs(path)):
        sys.exit(f"{record['id']} is already recorded; pass --force to record it again")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"recorded {record['id']} ({record['kind']}) in {path.relative_to(ROOT)}")


# local runs


def newest_trace() -> Path:
    directory = Path(os.environ.get("TRACE_DIR") or ROOT / "agent" / "traces")
    traces = sorted(directory.glob("run-*.jsonl"), key=lambda path: path.stat().st_mtime)
    if not traces:
        sys.exit(f"no run-*.jsonl trace in {directory}; pass --trace")
    return traces[-1]


def summarise_trace(path: Path) -> dict:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a run killed mid-write leaves a partial last line
    workers: dict[str, dict] = {}
    totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    errors: dict[str, int] = {}
    stops, limits, result = [], set(), None
    for event in events:
        worker, kind = event.get("worker", "?"), event.get("kind", "?")
        if worker == "result" and kind == "done":
            result = event.get("result")
            continue
        if kind == "view":
            continue
        if kind.endswith(("_error", "_refused")):
            errors[kind] = errors.get(kind, 0) + 1
        stats = workers.setdefault(worker, {"starts": 0, "done": 0, "stopped": 0, "requests": 0, "seconds": 0.0})
        if kind == "start":
            stats["starts"] += 1
        elif kind == "done" and "requests" in event:
            stats["done"] += 1
            stats["requests"] += event["requests"]
            stats["seconds"] = round(stats["seconds"] + event.get("seconds", 0), 1)
            for key in totals:
                totals[key] += event.get(key, 0)
        elif kind == "stopped":
            stats["stopped"] += 1
            stats["seconds"] = round(stats["seconds"] + event.get("seconds", 0), 1)
            reason = str(event.get("reason", ""))
            stops.append(f"{worker}: {reason.split(':')[0]}")
            limits.update(int(n) for n in re.findall(r"request_limit of (\d+)", reason))
    workers = {name: stats for name, stats in workers.items() if stats["starts"] or stats["done"]}
    try:
        started = dt.datetime.strptime(path.stem, "run-%Y%m%d-%H%M%S")  # local time, as tracing.py names it
    except ValueError:
        started = dt.datetime.fromtimestamp(path.stat().st_mtime)
    outcome = {"submitted": bool(result and result.get("submitted")), "errors": errors, "stops": stops}
    if result:
        outcome["confirmation"] = result.get("confirmation")
        outcome["omitted_positions"] = len(result.get("omitted_positions") or [])
    elif events:
        outcome["finished"] = False  # no result event: crashed, killed or still running
    return {
        "id": "local-" + path.stem.removeprefix("run-"),
        "kind": "local",
        "started_at": utc_iso(started),
        "trace": path.name,
        "duration_s": round(max((e.get("t", 0) for e in events), default=0), 1),
        **totals,
        "settings": {**agent_config(), "request_limits_hit": sorted(limits)},
        "workers": workers,
        "outcome": outcome,
    }


# platform runs


def atira(*args: str) -> dict:
    command = ["atira", *args, "--json"]
    try:
        done = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        sys.exit('atira not found; export PATH="$HOME/.local/bin:$PATH"')
    except subprocess.TimeoutExpired:
        sys.exit(f"{' '.join(command)} timed out")
    try:
        data = json.loads(done.stdout)
    except ValueError:
        sys.exit(f"{' '.join(command)} gave no JSON: {(done.stderr or done.stdout).strip()[:300]}")
    if isinstance(data, dict) and data.get("error") and "run_id" not in data and "submission_id" not in data:
        sys.exit(f"{' '.join(command)}: {data['error']}")
    return data


def resolve_rehearsal(rehearsal_id: str) -> str:
    for test_run in atira("status").get("test_runs", []):
        if test_run.get("rehearsal_id") == rehearsal_id:
            return test_run["run_id"]
    sys.exit(f"{rehearsal_id} is not among the team's latest test runs; pass its run_ id")


def run_fields(run: dict) -> dict:
    """The fields we keep from one run, from `atira status run_...` or a submission's runs list."""
    score = run.get("score") or {}
    started, finished = run.get("started_at"), run.get("finished_at")
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "started_at": from_ms(started),
        "duration_s": round((finished - started) / 1000, 1) if started and finished else None,
        "requests": run.get("calls"),
        "input_tokens": run.get("input_tokens"),
        "output_tokens": run.get("output_tokens"),
        "platform": {
            "eligible": score.get("eligible"),
            "quality": score.get("quality"),
            "breakdown": points_only(score.get("breakdown")),
            "cost_usd": round(run["cost_micro_usd"] / 1e6, 6) if run.get("cost_micro_usd") is not None else None,
            "scored_duration_s": round(score["duration_ms"] / 1000, 1) if score.get("duration_ms") else None,
            "error": run.get("error"),
            "failure_class": run.get("failure_class"),
            "scenario_id": run.get("scenario_id"),
        },
    }


def points_only(breakdown):
    """The score breakdown without the answer values it echoes ("got"), which are site data; points and position
    numbers stay."""
    if isinstance(breakdown, dict):
        return {key: points_only(value) for key, value in breakdown.items() if key not in ("got", "expected")}
    if isinstance(breakdown, list):
        return [points_only(value) for value in breakdown]
    return breakdown


def platform_record(run_id: str) -> dict:
    if run_id.startswith("reh_"):
        run_id = resolve_rehearsal(run_id)
    if run_id.startswith("sub_"):
        submission = atira("status", run_id)
        runs = submission.get("runs") or []
        record = {
            "id": run_id,
            "kind": "submission",
            "submission_id": run_id,
            "review_status": submission.get("review_status"),
            "findings": submission.get("findings"),
            "package_sha256": (submission.get("package") or {}).get("sha256"),
        }
        if runs:
            fields = run_fields(runs[0])
            detail = atira("status", runs[0]["run_id"]) if runs[0].get("run_id") else {}
            for key, value in run_fields(detail).items() if detail else ():
                if key == "platform":
                    fields["platform"].update({k: v for k, v in value.items() if v is not None})
                elif fields.get(key) is None:
                    fields[key] = value
            record.update(fields)
        else:
            record.update(started_at=from_ms(submission.get("created_at")), status=submission.get("status"))
        return record
    run = atira("status", run_id)
    kind = "test-run" if run.get("kind") == "rehearsal" else "submission"
    return {"id": run_id, "kind": kind, **run_fields(run)}


# report


def cell(value) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def outcome_text(run: dict) -> str:
    if run["kind"] == "local":
        outcome = run.get("outcome", {})
        parts = [
            "signed"
            if outcome.get("submitted")
            else ("no result" if outcome.get("finished") is False else "not submitted")
        ]
        if outcome.get("omitted_positions"):
            parts.append(f"{outcome['omitted_positions']} omitted")
        if outcome.get("stops"):
            parts.append(f"{len(outcome['stops'])} stops")
        errors = sum((outcome.get("errors") or {}).values())
        if errors:
            parts.append(f"{errors} errors/refusals")
        return ", ".join(parts)
    platform = run.get("platform") or {}
    parts = [run.get("status") or "?"]
    if run.get("review_status") and run.get("review_status") != "passed":
        parts.append(f"review {run['review_status']}")
    if platform.get("eligible"):
        parts.append("eligible")
    if platform.get("cost_usd") is not None:
        parts.append(f"${platform['cost_usd']:.3f}")
    if platform.get("failure_class") or platform.get("error"):
        parts.append(platform.get("failure_class") or platform["error"])
    return ", ".join(parts)


# Environment switches that change how the agent behaves; recorded with every run so variants stay comparable.
OVERRIDE_PREFIXES = ("AGENT_", "LLM_", "THINKING", "MAX_STEPS")


SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)


def overrides() -> dict[str, str]:
    return {key: "<redacted>" if SECRET.search(key) else value
            for key, value in sorted(os.environ.items()) if key.startswith(OVERRIDE_PREFIXES)}


def report() -> None:
    latest: dict[str, dict] = {}
    for run in load_runs():
        latest[run["id"]] = run  # a later record of the same id wins
    runs = sorted(latest.values(), key=lambda run: run.get("started_at") or "", reverse=True)
    rows = [
        "| Date (UTC) | Kind | Version · experiment | Commit | Duration | Tokens in/out | Quality | Outcome | Note |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for run in runs:
        commit = (run.get("commit") or "") + ("*" if run.get("dirty") else "")
        duration = run.get("duration_s")
        tokens = (
            f"{run['input_tokens'] / 1000:.0f}k / {run['output_tokens'] / 1000:.1f}k"
            if run.get("input_tokens") is not None and run.get("output_tokens") is not None
            else ""
        )
        quality = (run.get("platform") or {}).get("quality")
        date = (run.get("started_at") or "")[:16].replace("T", " ")
        rows.append(
            "| "
            + " | ".join(
                cell(value)
                for value in (
                    date,
                    f"{run['kind']} `{run['id']}`",
                    " · ".join(part for part in (run.get("version"), run.get("experiment")) if part),
                    commit,
                    (f"{duration / 60:.1f} min" if duration >= 60 else f"{duration:.0f} s") if duration else "",
                    tokens,
                    quality,
                    outcome_text(run),
                    run.get("note"),
                )
            )
            + " |"
        )
    table = "\n".join(rows)
    DOC.write_text(f"{DOC_HEADER}\n{table}\n", encoding="utf-8")
    print(f"wrote {DOC.relative_to(ROOT)} ({len(runs)} runs)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("local", "platform"):
        command = commands.add_parser(name)
        if name == "local":
            command.add_argument("--trace", type=Path, help="trace file (default: the newest)")
        else:
            command.add_argument("run_id", help="run_, reh_ (test run) or sub_ (submission) id")
        command.add_argument("--version", help="agent version label, e.g. v1")
        command.add_argument("--experiment", help="what this run varies on top of the version, e.g. thinking-off")
        command.add_argument("--note", help="free-form note: what changed, what to look at")
        command.add_argument("--commit", help="record this commit instead of the current HEAD")
        command.add_argument("--dirty", choices=("yes", "no"), help="with --commit: agent/ had uncommitted changes")
        command.add_argument("--force", action="store_true", help="record an id that is already recorded")
    commands.add_parser("report")
    args = parser.parse_args()
    if args.command == "report":
        report()
        return
    record = summarise_trace(args.trace or newest_trace()) if args.command == "local" else platform_record(args.run_id)
    meta = {
        "id": record.pop("id"),
        "kind": record.pop("kind"),
        "started_at": record.pop("started_at", None),
        "recorded_at": utc_iso(dt.datetime.now(UTC)),
        "version": args.version,
        "experiment": args.experiment,
        "overrides": overrides(),
        **git_info(args.commit, args.dirty),
        "note": args.note,
    }
    append_run({**meta, **record}, args.force)


if __name__ == "__main__":
    main()
