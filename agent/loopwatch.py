"""
Make a hung run diagnosable: a daemon thread watches a heartbeat the event loop updates every second.

When the loop has not beaten for STALE_SECONDS (synchronous work or a blocking call on the loop), every thread's
stack goes to stdout once (faulthandler, so it lands in the run log), at most every REPEAT_SECONDS. When the loop
still runs but the trace has been silent for SILENT_SECONDS (every task waiting on something that never comes),
the stacks of all asyncio tasks go to stdout instead.
"""
import asyncio
import faulthandler
import sys
import threading
import time

STALE_SECONDS = 20.0
REPEAT_SECONDS = 60.0
SILENT_SECONDS = 300.0


class Watchdog:
    def __init__(self, activity=None, stale: float = STALE_SECONDS, repeat: float = REPEAT_SECONDS,
                 silent: float = SILENT_SECONDS, out=None):
        self.activity = activity  # a callable: time.monotonic() of the run's latest trace event
        self.stale, self.repeat, self.silent = stale, repeat, silent
        self.out = out or sys.stdout
        self.beat = time.monotonic()
        self.dumps = 0
        self._stop = threading.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> "Watchdog":
        self._task = asyncio.get_running_loop().create_task(self._heartbeat(), name="watchdog")
        threading.Thread(target=self._watch, name="loop-watchdog", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()

    async def _heartbeat(self) -> None:
        last_tasks_dump = 0.0
        while True:
            self.beat = time.monotonic()
            if self.activity is not None:
                quiet = self.beat - self.activity()
                if quiet > self.silent and self.beat - last_tasks_dump > self.silent:
                    last_tasks_dump = self.beat
                    self._dump_tasks(quiet)
            await asyncio.sleep(1.0)

    def _dump_tasks(self, quiet: float) -> None:
        tasks = [t for t in asyncio.all_tasks() if t is not self._task]
        print(f"[watchdog] no trace event for {quiet:.0f} s; stacks of {len(tasks)} asyncio tasks follow",
              file=self.out, flush=True)
        for task in tasks:
            try:
                task.print_stack(limit=12, file=self.out)
            except Exception as error:
                print(f"[watchdog] {task.get_name()}: {error}", file=self.out)
        self.out.flush()

    def _watch(self) -> None:
        last_dump = 0.0
        while not self._stop.wait(1.0):
            now = time.monotonic()
            blocked = now - self.beat
            if blocked > self.stale and now - last_dump > self.repeat:
                last_dump = now
                self.dumps += 1
                try:
                    print(f"[watchdog] the event loop has not run for {blocked:.0f} s; thread stacks follow",
                          file=self.out, flush=True)
                    faulthandler.dump_traceback(file=self.out, all_threads=True)
                    self.out.flush()
                except Exception:
                    pass
