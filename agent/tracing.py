"""A run's trace: one JSON line per event in a file, and a short line on stdout for the run logs."""
import json
import os
import time
from pathlib import Path


class Trace:
    def __init__(self, directory: str | None = None):
        self.started = self.last = time.monotonic()
        directory = directory or os.environ.get("TRACE_DIR", "traces")
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.path = Path(directory) / time.strftime("run-%Y%m%d-%H%M%S.jsonl")
        self.file = self.path.open("a", encoding="utf-8")

    def event(self, worker: str, kind: str, quiet: bool = False, **data) -> None:
        self.last = time.monotonic()
        seconds = round(self.last - self.started, 2)
        self.file.write(json.dumps({"t": seconds, "worker": worker, "kind": kind, **data}, ensure_ascii=False) + "\n")
        self.file.flush()
        if quiet:  # file only, e.g. the full page views
            return
        brief = " ".join(f"{k}={str(v)[:120]!r}" if isinstance(v, str) else f"{k}={v}" for k, v in data.items())
        print(f"[{seconds:7.1f}s] {worker:<15} {kind:<12} {brief}", flush=True)
