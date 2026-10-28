"""Starts loadgen.py in a harness clone, dispatches its burst and accounts for its lines.

loadgen.py runs the worker SDK's own loop in a one-off worker container. This side starts that
container with `./neops compose -- run --rm --no-deps`, waits for its block to be registered,
publishes and runs the one-step global workflow on the block, and reads back what the SDK said it
could not deliver. `account` sets that against the records the store holds for the loadgen worker.
It refuses any directory but a clone run_scenario.py left behind.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import loadgen
import workflows
from logrecords import seq_series_key
from run_scenario import clone_refusal

# The file this driver parses the markers of, not the clone's copy: that one is whatever was
# committed when the clone was made.
LOADGEN_SOURCE = Path(loadgen.__file__).resolve()
LOADGEN_MOUNT = "/loadgen.py"
# The image's own venv: its CMD is `uv run --no-sync neops_worker` from /app.
WORKER_PYTHON = "/app/.venv/bin/python"
BURST_WORKFLOW = "log_burst"
BURST_WORKFLOW_VERSION = (1, 0, 0)
# Registration retries for up to the SDK's STARTUP_RETRY_TIMEOUT, 120 s.
READY_TIMEOUT = 180.0
# The worker polls every 10 s, and the acquire job runs before the execute job.
BURST_START_TIMEOUT = 90.0
# After the burst: loadgen's settle, the SDK's graceful shutdown and its final flush. Steps call
# `finish` once their fault is over, so the engine is back and loadgen does not wait for it.
EXIT_SLACK = loadgen.SETTLE_SECONDS + 120.0
TAIL_LINES = 20


class LoadgenError(RuntimeError):
    pass


def container_name(project: str) -> str:
    return f"{project}-loadgen-{uuid.uuid4().hex[:8]}"


def loadgen_command(container: str, rate: int, seconds: float) -> list[str]:
    mount = f"{LOADGEN_SOURCE}:{LOADGEN_MOUNT}:ro"
    return [
        "./neops", "compose", "--", "run", "--rm", "--no-deps", "-T", "--name", container, "-v", mount,
        "worker", WORKER_PYTHON, LOADGEN_MOUNT, "--rate", str(rate), "--seconds", str(seconds),
    ]  # fmt: skip


def burst_workflow_document() -> dict:
    major, minor, patch = BURST_WORKFLOW_VERSION
    return {
        "type": "workflow",
        "package": workflows.WORKFLOW_PACKAGE,
        "name": BURST_WORKFLOW,
        "label": BURST_WORKFLOW,
        "majorVersion": major,
        "minorVersion": minor,
        "patchVersion": patch,
        "description": "neops-docker-compose e2e: one burst of log lines from loadgen.py.",
        "seedEntity": "global",
        "steps": [
            {"type": "functionBlock", "label": "burst", "functionBlock": loadgen.BURST_FB, "parameters": {}}
        ],
    }


class LoadgenProcess:
    """The `compose run` client: its output echoed, kept, and searched for loadgen's markers."""

    def __init__(self, command: list[str], cwd: Path):
        print("+ " + " ".join(command), flush=True)
        self.lines: list[str] = []
        self._markers: dict[str, dict] = {}
        self._ended = False
        self._changed = threading.Condition()
        self._proc = subprocess.Popen(
            command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        for raw in self._proc.stdout:
            line = raw.rstrip("\n")
            print(f"loadgen| {line}", flush=True)
            marker = loadgen.parse_marker(line)
            with self._changed:
                self.lines.append(line)
                if marker:
                    self._markers[marker[0]] = marker[1]
                self._changed.notify_all()
        with self._changed:
            self._ended = True
            self._changed.notify_all()

    def wait_marker(self, name: str, timeout: float) -> dict | None:
        with self._changed:
            self._changed.wait_for(lambda: name in self._markers or self._ended, timeout)
            return self._markers.get(name)

    def wait(self, timeout: float) -> int | None:
        try:
            code = self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        self._reader.join(5)
        return code

    def tail(self) -> str:
        return " | ".join(self.lines[-TAIL_LINES:])


@dataclass
class BurstOutcome:
    worker_id: str | None
    summary: loadgen.Summary | None
    run: workflows.Run | None
    exit_code: int | None
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        run = f"run {self.run.execution_id} {self.run.status}" if self.run else "no run"
        return f"worker {self.worker_id}, exit {self.exit_code}, {run}, {self.summary}"


class BurstLoad:
    """One loadgen worker in a harness clone, and the one run of its burst."""

    def __init__(self, clone: Path, project: str, engine: workflows.Engine, rate: int, seconds: float):
        self.clone = clone
        self.engine = engine
        self.rate = rate
        self.seconds = seconds
        self.container = container_name(project)
        self.worker_id: str | None = None
        self._process: LoadgenProcess | None = None
        self._execution_id: str | None = None
        self._triggered = 0.0

    def start(self) -> None:
        """Returns once the burst's lines are flowing; a loadgen that got that far is killed."""
        try:
            self.launch()
            self.trigger()
        except Exception:
            self.kill()
            raise

    def launch(self) -> str:
        """Start the container; the worker id once its block is registered."""
        if refusal := clone_refusal(self.clone, os.environ):
            raise LoadgenError(refusal)
        command = loadgen_command(self.container, self.rate, self.seconds)
        self._process = LoadgenProcess(command, self.clone)
        ready = self._process.wait_marker(loadgen.READY, READY_TIMEOUT) or {}
        if not ready.get("worker_id"):
            raise LoadgenError(
                f"loadgen did not register within {READY_TIMEOUT:.0f}s: {self._process.tail()}"
            )
        self.worker_id = ready["worker_id"]
        return self.worker_id

    def trigger(self) -> None:
        workflow_id = workflows.publish_document(self.engine, burst_workflow_document())
        self._triggered = time.monotonic()
        self._execution_id = workflows.start_execution(self.engine, workflow_id, {})
        if self._process.wait_marker(loadgen.BURST, BURST_START_TIMEOUT) is None:
            raise LoadgenError(f"the burst did not start within {BURST_START_TIMEOUT:.0f}s")

    def kill(self) -> None:
        print(f"+ docker kill -s KILL {self.container}", flush=True)
        subprocess.run(["docker", "kill", "-s", "KILL", self.container], capture_output=True, check=False)

    def finish(self) -> BurstOutcome:
        """Wait for loadgen to exit, killing it when it overstays, and read its run's status."""
        errors: list[str] = []
        code = self._process.wait(self.seconds + EXIT_SLACK) if self._process else None
        if self._process and code is None:
            errors.append(f"loadgen did not exit within {self.seconds + EXIT_SLACK:.0f}s; killed")
            self.kill()
            code = self._process.wait(30)
        lines = self._process.lines if self._process else []
        return BurstOutcome(self.worker_id, loadgen.summary_of(lines), self._run(code, errors), code, errors)

    def _run(self, exit_code: int | None, errors: list[str]) -> workflows.Run | None:
        """A loadgen that died leaves its run unsettled for the engine to fail much later."""
        if self._execution_id is None:
            return None
        try:
            if exit_code == 0:
                status = workflows.wait_settled(self.engine, self._execution_id)
            else:
                status = workflows.status_of(self.engine, self._execution_id)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}"[:200])
            status = "?"
        return workflows.Run(self._execution_id, status, time.monotonic() - self._triggered)


@dataclass(frozen=True)
class Accounting:
    """One loadgen worker's lines in the store, set against the `seq` the SDK numbered them by."""

    assigned: int
    received: int
    duplicates: int
    burst_received: int
    newest_burst: int | None
    drops_reported: int
    """The sum of the N of every stored `dropped N log lines` line."""

    @property
    def missing(self) -> int:
        return self.assigned - self.received


def account(records: Iterable[Mapping]) -> Accounting:
    """Every series starts at seq 0 and its last line goes out after the outage, so the highest
    seq stored tells how many the SDK numbered."""
    records = list(records)
    series: dict[tuple[str, ...], Counter[int]] = defaultdict(Counter)
    for record in records:
        series[seq_series_key(record)][int(record["seq"])] += 1
    messages = [str(record.get("message", "")) for record in records]
    bursts = {index for index in map(loadgen.burst_index, messages) if index is not None}
    return Accounting(
        assigned=sum(max(seqs) + 1 for seqs in series.values()),
        received=sum(len(seqs) for seqs in series.values()),
        duplicates=sum(n - 1 for seqs in series.values() for n in seqs.values()),
        burst_received=len(bursts),
        newest_burst=max(bursts, default=None),
        drops_reported=sum(int(m.group(1)) for m in map(loadgen.DROPPED_PATTERN.match, messages) if m),
    )
