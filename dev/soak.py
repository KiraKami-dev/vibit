"""
Keep a few sandbox test runs of the current main going, to find errors early; one summary line per finished run.

Usage, from the repo root (stdlib only):
    python3 dev/soak.py [--slots 2] [--version v2] [--note "..."]

Every new run packs a clean `git archive` of HEAD's agent/, so fixes committed meanwhile go into the next run.
Finished runs are recorded with dev/track.py (experiment "soak") and summarised in experiments/soak.log (gitignored):
quality, minutes, cost, and the problems, stops and failed phases the run's log shows. Stop it with Ctrl-C; runs
already started keep going on the platform.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "experiments" / "soak.log"
SIGNAL = re.compile(r"\] (\S+)\s+(problem|stopped|last_chance|summary_check|spec_done|decision_disputed)\s+(.*)")


def atira(*args: str, timeout: int = 180) -> str:
    try:
        return subprocess.run(["atira", *args], cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return ""


def start_run() -> tuple[str, str] | None:
    """A test run of HEAD's agent/, packed from a clean export; (run id, commit)."""
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                            text=True).stdout.strip()
    folder = Path(tempfile.mkdtemp(prefix="soak-"))
    archive = subprocess.run(["git", "archive", "HEAD", "agent"], cwd=ROOT, capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(folder)], input=archive, check=True)
    out = subprocess.run(["atira", "test-run", "./agent", "--no-open"], cwd=folder, stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=600).stdout
    found = re.search(r"run_[0-9a-f]+", out)
    if not found:
        print(f"could not start a run: {out.strip()[:300]}", flush=True)
        return None
    return found.group(0), commit


def state(run: str) -> str:
    first = atira("status", run).splitlines()[:1]
    return first[0] if first else ""


def summarise(run: str, commit: str, version: str, note: str) -> str:
    status = atira("status", run, "--json")
    try:
        score = (json.loads(status).get("score") or {})
    except ValueError:
        score = {}
    logs = atira("logs", run, timeout=300)
    signals = []
    for line in logs.splitlines():
        match = SIGNAL.search(line)
        if match and not (match.group(2) == "summary_check" and "matches=True" in line):
            signals.append(f"{match.group(1)} {match.group(2)}: {match.group(3)[:140]}")
    bid = "no bid" if "without submitting" in atira("status", run) or "result=None" in logs else "signed"
    subprocess.run([sys.executable, str(ROOT / "dev" / "track.py"), "platform", run, "--version", version,
                    "--experiment", "soak", "--commit", commit, "--dirty", "no", "--note", note],
                   cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
    minutes = (score.get("duration_ms") or 0) / 60000
    cost = (score.get("cost_micro_usd") or 0) / 1e6
    head = f"{run} {commit} quality={score.get('quality')} {minutes:.1f}min ${cost:.3f} {bid}"
    return head + ("\n    " + "\n    ".join(signals[:12]) if signals else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--slots", type=int, default=2, help="test runs to keep going (team limit is 5)")
    parser.add_argument("--version", default="v2")
    parser.add_argument("--note", default="soak run of main")
    args = parser.parse_args()
    LOG.parent.mkdir(exist_ok=True)
    active: dict[str, str] = {}
    while True:
        for run, commit in list(active.items()):
            line = state(run)
            if re.search(r"\((test run|scored)\): (finished|failed|cancelled|timed|scored)", line):
                summary = summarise(run, commit, args.version, args.note)
                with LOG.open("a", encoding="utf-8") as file:
                    file.write(time.strftime("%H:%M ") + summary + "\n")
                print(summary, flush=True)
                del active[run]
        while len(active) < args.slots:
            started = start_run()
            if not started:
                break
            active[started[0]] = started[1]
            print(f"started {started[0]} at {started[1]}", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
