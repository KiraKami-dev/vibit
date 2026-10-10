"""A run's trace: one JSON line per event in a file, and a short line on stdout for the run logs."""
import json
import os
import time
from pathlib import Path


CURRENT: "Trace | None" = None  # the run's trace, for the summary at the very end


def print_summary() -> None:
    """The current run's phase and failure events in one block (no-op before a trace exists)."""
    if CURRENT is not None:
        CURRENT.summary()


class Trace:
    def __init__(self, directory: str | None = None):
        global CURRENT
        CURRENT = self
        self.key: list[str] = []
        self.started = self.last = time.monotonic()
        directory = directory or os.environ.get("TRACE_DIR", "traces")
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.path = Path(directory) / time.strftime("run-%Y%m%d-%H%M%S.jsonl")
        self.file = self.path.open("a", encoding="utf-8")

    # events that mark a phase or a failure; repeated in one block at the end of the run (summary())
    KEY_KINDS = {"start", "done", "stopped", "problem", "start_failed", "spec_done", "tender_facts", "mail_kind",
                 "answer", "summary_check", "last_chance", "crashed", "decision_disputed", "helper_error",
                 "gap", "gap_filled", "gap_round", "gap_open", "gap_no_time"}

    def event(self, worker: str, kind: str, quiet: bool = False, **data) -> None:
        self.last = time.monotonic()
        seconds = round(self.last - self.started, 2)
        self.file.write(json.dumps({"t": seconds, "worker": worker, "kind": kind, **data}, ensure_ascii=False) + "\n")
        self.file.flush()
        if quiet:  # file only, e.g. the full page views
            return
        brief = " ".join(f"{k}={str(v)[:120]!r}" if isinstance(v, str) else f"{k}={v}" for k, v in data.items())
        print(f"[{seconds:7.1f}s] {worker:<15} {kind:<12} {brief}", flush=True)
        if kind in self.KEY_KINDS:
            self.key.append(f"[{seconds:7.1f}s] {worker:<15} {kind:<12} {brief[:150]}")

    def summary(self, limit: int = 120) -> None:
        """Print the phase and failure events again in one block: the platform keeps only the last few hundred
        lines of a run's output, so without this the early phases are missing from every run log."""
        lines = self.key if len(self.key) <= limit else self.key[:20] + ["  ..."] + self.key[-(limit - 21):]
        print("===== run summary =====\n" + "\n".join(lines) + "\n===== end of run summary =====", flush=True)
