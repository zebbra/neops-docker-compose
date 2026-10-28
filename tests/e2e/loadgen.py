#!/usr/bin/env python3
"""A log burst through the worker SDK's real push sink, run inside the worker image.

    ./neops compose -- run --rm --no-deps -T -v $PWD/tests/e2e/loadgen.py:/loadgen.py:ro \
        worker /app/.venv/bin/python /loadgen.py --rate 300 --seconds 60

It runs the SDK's own worker loop, `neops_worker.main_async`, with one function block that only
it registers, so no other worker takes its jobs. A job of that block logs `--rate` lines a second
for `--seconds` through the job's logger: every line takes the production path through the push
sink's buffer, its overflow and its retries. The harness drives it with loadgen_driver.py, which
also runs the workflow that dispatches the job.

On stdout it prints `LOADGEN READY` once its block is registered, `LOADGEN BURST` when the job
starts, and `LOADGEN SUMMARY` at exit, each with a JSON object. The counts in the summary are
what the SDK itself logged about lines it did not deliver.

Only the standard library is imported at module level: the harness and the unit tests import
this file for its constants and parsers, without the SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BURST_PACKAGE = "fb.e2e.neops.io"
BURST_NAME = "log_burst"
BURST_VERSION = (0, 1, 0)
BURST_FB = f"{BURST_PACKAGE}/{BURST_NAME}:{'.'.join(map(str, BURST_VERSION))}"
BURST_LINE = "log_burst line {}"
BURST_LINE_PATTERN = re.compile(r"^log_burst line (\d+)$")
WORKER_NAME = "e2e-loadgen"
# Stdout only: the push level stays the SDK's, so what reaches the sink is what production pushes.
STDOUT_LEVEL = "warn"
MARKER = "LOADGEN"
READY, BURST, SUMMARY = "READY", "BURST", "SUMMARY"
TICK_SECONDS = 0.1
# After the burst, once the engine answers: time for the job's result and the backlog an outage
# left to go out before the graceful stop, whose own final flush is bounded at 5 s. A flush that
# met the outage may sit in a retry backoff of up to 16 s first, and /health answers before the
# outbox takes lines.
SETTLE_SECONDS = 45.0
ENGINE_WAIT_SECONDS = 300.0
ENGINE_HEALTH = "/health"
# Without a job for this long past the burst's length, the worker stops on its own.
IDLE_LIMIT_SECONDS = 300.0


@dataclass(frozen=True)
class SdkMessage:
    """An SDK log message as its f-string's literal pieces, a number between each two."""

    module: str
    pieces: tuple[str, ...]

    @property
    def pattern(self) -> re.Pattern[str]:
        return re.compile("^" + r"(\d+)".join(map(re.escape, self.pieces)))

    def logged_in(self, source: str) -> bool:
        return all(piece in source for piece in self.pieces)


# A reworded one of these would count 0 without a sound, so loadgen checks the SDK still logs them.
# The rest fail a run loudly: no READY, no retry status, no drop warning in the store.
QUIET_NOTICES: dict[str, SdkMessage] = {
    "unpushed": SdkMessage(
        "neops_worker_sdk.logger.push.pipeline", ("", " log line(s) were not pushed to the engine.")
    ),
    "refused": SdkMessage("neops_worker_sdk.logger.push.delivery", ("The engine refused ", " log line(s)")),
    "lease_refused": SdkMessage(
        "neops_worker_sdk.logger.push.delivery", ("The engine refuses log lines of job ",)
    ),
}
# What the SDK logs about its own work, read by the observer sink (neops_worker_sdk/logger/push,
# neops_worker_sdk/worker/worker_registration.py, neops_worker_sdk/registry/registry.py).
DROPPED_PATTERN = re.compile(r"^dropped (\d+) log lines \(buffer full\)$")
SDK_NOTICES: dict[str, re.Pattern[str]] = {
    "worker_id": re.compile(r"^Worker registered \(uuid=([0-9a-fA-F-]+)"),
    "registered": re.compile(r"^All \d+ function block\(s\) registered successfully"),
    "dropped": DROPPED_PATTERN,
    **{name: message.pattern for name, message in QUIET_NOTICES.items()},
    "retry": re.compile(r"^Log push failed \(\w+: \((\d{3})\)"),
}


@dataclass
class Summary:
    """What loadgen sent and what the SDK said it could not deliver."""

    worker_id: str | None = None
    sent: int = 0
    burst_seconds: float = 0.0
    dropped: int = 0
    unpushed: int = 0
    refused: int = 0
    lease_refused: int = 0
    retry_statuses: list[int] = field(default_factory=list)
    exit_code: int | None = None

    @property
    def otherwise_lost(self) -> int:
        """Lines gone for any reason but the buffer's overflow."""
        return self.unpushed + self.refused + self.lease_refused

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Summary:
        known = {key: payload[key] for key in cls.__dataclass_fields__ if key in payload}
        return cls(**known)


class Notices:
    """The SDK's own account of its pushes, kept from its log messages."""

    def __init__(self) -> None:
        self.summary = Summary()
        self.registered = False

    def observe(self, message: str) -> str | None:
        """The name of the notice `message` is, after counting it, or None."""
        for name, pattern in SDK_NOTICES.items():
            match = pattern.match(message)
            if match:
                self._count(name, match)
                return name
        return None

    def _count(self, name: str, match: re.Match[str]) -> None:
        summary = self.summary
        if name == "worker_id":
            summary.worker_id = match.group(1)
        elif name == "registered":
            self.registered = True
        elif name == "lease_refused":
            summary.lease_refused += 1
        elif name == "retry":
            summary.retry_statuses = sorted({*summary.retry_statuses, int(match.group(1))})
        else:
            setattr(summary, name, getattr(summary, name) + int(match.group(1)))


def marker_line(name: str, payload: Mapping[str, Any]) -> str:
    return f"{MARKER} {name} {json.dumps(payload, sort_keys=True)}"


def parse_marker(line: str) -> tuple[str, dict] | None:
    parts = line.strip().split(" ", 2)
    if len(parts) != 3 or parts[0] != MARKER:
        return None
    try:
        payload = json.loads(parts[2])
    except ValueError:
        return None
    return (parts[1], payload) if isinstance(payload, dict) else None


def summary_of(lines: Iterable[str]) -> Summary | None:
    """The last summary among a run's output lines."""
    found = None
    for line in lines:
        marker = parse_marker(line)
        if marker and marker[0] == SUMMARY:
            found = Summary.from_payload(marker[1])
    return found


def burst_index(message: object) -> int | None:
    match = BURST_LINE_PATTERN.match(str(message))
    return int(match.group(1)) if match else None


def burst_total(rate: int, seconds: float) -> int:
    return round(rate * seconds)


def lines_due(rate: int, total: int, elapsed: float) -> int:
    """How many lines should be out `elapsed` seconds into the burst: on schedule, never ahead."""
    return min(total, int(rate * max(elapsed, 0.0)))


def announce(name: str, payload: Mapping[str, Any]) -> None:
    print(marker_line(name, payload), flush=True)


class Burst:
    """`rate` lines a second for `seconds`, logged once by the first job that runs it."""

    def __init__(self, rate: int, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.rate = rate
        self.seconds = seconds
        self.total = burst_total(rate, seconds)
        self.sent = 0
        self.elapsed = 0.0
        self.done = threading.Event()
        self._clock = clock

    async def emit(self, log: Callable[[str], None]) -> int:
        announce(BURST, {"rate": self.rate, "seconds": self.seconds})
        started = self._clock()
        while self.sent < self.total:
            due = lines_due(self.rate, self.total, self._clock() - started)
            while self.sent < due:
                log(BURST_LINE.format(self.sent))
                self.sent += 1
            await asyncio.sleep(TICK_SECONDS)
        self.elapsed = self._clock() - started
        self.done.set()
        return self.sent


def positive(kind: Callable[[str], float]) -> Callable[[str], Any]:
    def parse(text: str) -> float:
        value = kind(text)
        if value <= 0:
            raise argparse.ArgumentTypeError(f"{text} is not above 0")
        return value

    return parse


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate", type=positive(int), required=True, help="lines a second")
    ap.add_argument("--seconds", type=positive(float), required=True, help="length of the burst")
    return ap.parse_args(argv)


def refusal(environ: Mapping[str, str]) -> str | None:
    """Why this process is no place for the burst, or None."""
    if not environ.get("URL_BLACKBOARD"):
        return (
            "refusing: URL_BLACKBOARD is not set; run loadgen.py inside the worker image (see its docstring)"
        )
    if environ.get("NEOPS_LOG_PUSH_LEVEL", "").lower() == "off":
        return "refusing: NEOPS_LOG_PUSH_LEVEL=off, so no line would reach the push sink"
    return None


def sdk_source(module: str) -> str:
    try:
        spec = importlib.util.find_spec(module)
    except ModuleNotFoundError:
        return ""
    return Path(spec.origin).read_text() if spec and spec.origin else ""


def sdk_refusal(source_of: Callable[[str], str | None]) -> str | None:
    """Why the installed SDK would make loadgen's counts lie, or None."""
    gone = [
        name
        for name, message in QUIET_NOTICES.items()
        if not message.logged_in(source_of(message.module) or "")
    ]
    if not gone:
        return None
    return f"refusing: the SDK no longer logs the {', '.join(gone)} message(s); update loadgen.QUIET_NOTICES"


def prepare_environment(environ: MutableMapping[str, str]) -> None:
    """Read by the SDK when it is imported: no function block directory, so the burst block is
    the only one this worker registers, and its own name in the engine's worker list."""
    environ.pop("DIR_FUNCTION_BLOCKS", None)
    environ["WORKER_NAME"] = WORKER_NAME
    environ["NEOPS_LOG_LEVEL"] = STDOUT_LEVEL


def register_burst_block(burst: Burst) -> None:
    from neops_worker_sdk.function_block.function_block import (
        FunctionBlock,
        FunctionBlockAcquireResult,
        FunctionBlockParams,
        FunctionBlockResult,
        FunctionBlockResultData,
    )
    from neops_worker_sdk.registry.decorator import register_function_block
    from neops_worker_sdk.registry.registration import Registration

    class BurstParameters(FunctionBlockParams):
        pass

    class BurstResult(FunctionBlockResultData):
        sent: int

    @register_function_block(
        Registration(
            name=BURST_NAME,
            description="neops-docker-compose e2e: logs a burst of lines at a set rate.",
            package=BURST_PACKAGE,
            run_on="global",
            fb_type="execute",
            param_cls=BurstParameters,
            result_cls=BurstResult,
            version=BURST_VERSION,
        )
    )
    class LogBurst(FunctionBlock[BurstParameters, BurstResult]):
        async def acquire(self, params: Any) -> FunctionBlockAcquireResult:
            return FunctionBlockAcquireResult(success=True, message="nothing to acquire", acquires=None)

        async def run(self, params: BurstParameters, context: Any) -> FunctionBlockResult[BurstResult]:
            sent = await burst.emit(self.logger.info)
            return FunctionBlockResult(success=True, message=f"{sent} lines", data=BurstResult(sent=sent))


def observe_sdk(notices: Notices) -> None:
    """A loguru sink of its own, added after the SDK's import has reset loguru's sinks."""
    from loguru import logger

    def sink(message: Any) -> None:
        if notices.observe(str(message.record["message"])) == "registered":
            announce(READY, {"worker_id": notices.summary.worker_id})

    logger.add(sink, level="INFO", format="{message}")


def engine_answers(engine_url: str) -> bool:
    try:
        with urllib.request.urlopen(engine_url.rstrip("/") + ENGINE_HEALTH, timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


async def settle(engine_url: str) -> None:
    """The backlog of an engine outage goes out only once the engine is back."""
    deadline = time.monotonic() + ENGINE_WAIT_SECONDS
    while time.monotonic() < deadline and not await asyncio.to_thread(engine_answers, engine_url):
        await asyncio.sleep(1)
    await asyncio.sleep(SETTLE_SECONDS)


async def stop_after_burst(worker: asyncio.Task[int], burst: Burst, engine_url: str) -> None:
    """SIGTERM to itself once the burst is out and settled, or when no job came: the SDK's own
    graceful shutdown then finishes the job, unregisters and flushes."""
    deadline = time.monotonic() + burst.seconds + IDLE_LIMIT_SECONDS
    while not worker.done() and not burst.done.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
    if worker.done():
        return
    if burst.done.is_set():
        await settle(engine_url)
    os.kill(os.getpid(), signal.SIGTERM)


async def run_worker(burst: Burst, engine_url: str) -> int:
    from neops_worker_sdk.cli import neops_worker

    worker = asyncio.create_task(neops_worker.main_async())
    await stop_after_burst(worker, burst, engine_url)
    return await worker


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if reason := refusal(os.environ):
        print(reason, file=sys.stderr)
        return 2
    prepare_environment(os.environ)
    import neops_worker_sdk.cli.neops_worker  # noqa: F401  configures loguru before the observer joins

    if reason := sdk_refusal(sdk_source):
        print(reason, file=sys.stderr)
        return 2
    notices = Notices()
    observe_sdk(notices)
    burst = Burst(args.rate, args.seconds)
    register_burst_block(burst)
    code = asyncio.run(run_worker(burst, os.environ["URL_BLACKBOARD"]))
    summary = notices.summary
    summary.sent, summary.burst_seconds, summary.exit_code = burst.sent, round(burst.elapsed, 2), code
    announce(SUMMARY, asdict(summary))
    return code


if __name__ == "__main__":
    sys.exit(main())
