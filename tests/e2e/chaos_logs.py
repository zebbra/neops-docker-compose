"""The log-store chaos steps, C1-C16 of the logging test plan (§5), on a stack with
compose.logs.yaml. C1 rides in chaos.py's `kill` step and C4b in its `env` step; C13 needs a
skewed worker clock and is deferred (D8). C15 only reports. `loadgen` checks the generator the
steps "under load" use, before they rely on it.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

import inside
import logrecords
import workflows
from chaos_kit import (
    BURST_RATE,
    LoadResult,
    Stack,
    burst_load,
    compose,
    docker,
    doctor_green,
    drained,
    engine_env,
    engine_partitioned,
    engine_stopped,
    env_values,
    failing_probes,
    make_load,
    outbox_attempts_and_rows,
    outbox_rows,
    recreate,
    state_is,
    store_paused,
    store_records,
    store_stopped,
    utc_now,
    version_logs,
    wait_for,
    wait_healthy,
    wait_logs,
    worker_records,
)
from loadgen_driver import Accounting, BurstOutcome, account
from run_scenario import CONNECT, Report, neops

from neops_compose.doctor import LOGS_PROBE

OUTAGE_SECONDS = 120
PARTITION_SECONDS = 60
DOUBLE_OUTAGE_SECONDS = 30
STATE_TIMEOUT = 90.0
REPLAY_TIMEOUT = 600.0
READABLE_SAMPLE = 3
# The engine answers its own unknown routes 404, which the push classifies as `configuration`.
MISCONFIGURED_URL = inside.ENGINE_IN_STACK
VICTORIALOGS_PUSH = "/insert/loki/api/v1/push?disable_message_parsing=1"
BLACKHOLE_URL = "http://10.255.255.1:9428"
OUTBOX_CAP = "2000"
OUTBOX_CAP_THREADS = 4
OUTBOX_DROP_MARKER = "dropped its oldest lines"
# The newest 0.2 worker. SDK 0.2.0-beta.6 and later ask the engine and exit on an incompatible
# answer; 0.2.0-beta.5 never asks.
OLD_WORKER_IMAGE = "quay.io/zebbra/neops-worker-sdk:0.2.0-beta.8"
OLD_ENGINE_IMAGE = "quay.io/zebbra/neops-workflow-engine:0.43.0-beta.4"
# What neops_worker.py of 0.2.0-beta.8 logs when it refuses to start, lowercased.
REFUSAL_MARKER = "is not compatible with this engine"
# An engine built without NEOPS_VERSION reports `develop` and answers every client compatible,
# so C12a judges only an engine stamped with the major.minor compose.yaml pins.
ENGINE_PIN = re.compile(r"neops-workflow-engine:\$\{NEOPS_ENGINE_TAG:-([^}]+)\}")
# A seq hole of the engine is expected wherever its store fails: a failed probe takes a seq
# and is never spooled. The worker's series must stay whole.
OUTAGE_HOLE_SERVICES = (logrecords.WORKER_SERVICE,)
WORKER_EXIT_TIMEOUT = 120.0
SLOW_STORE_RUNS = 10
SLOW_STORE_PAUSE = 100.0
SLOW_STORE_SLACK = 1.0
FINE_POLL = 0.25
STORED_TIMEOUT = 60.0
LOADGEN_CHECK_SECONDS = 60.0
# The SDK sleeps a tick between batches of lines, so a burst runs a little past its length.
RATE_SLACK_SECONDS = 2.0
# 400 lines/s for 90 s against the SDK's 10,000-line buffer, with the engine gone for 75 s.
OVERFLOW_RATE = 400
OVERFLOW_SECONDS = 90.0
ENGINE_DOWN_SECONDS = 75.0
KILL_BURST_SECONDS = 60.0
KILL_AFTER_SECONDS = 20.0
ONLINE_TIMEOUT = 60.0
FRESH_RATE = 10
FRESH_SECONDS = 5.0
RETRIED_STATUS = 503


@dataclass
class Outage:
    start: str
    before: dict
    during: dict
    after: dict
    recovered: bool
    replay_seconds: float
    load: LoadResult


def outage_under_load(
    report: Report,
    stack: Stack,
    fault: Callable[[], AbstractContextManager],
    seconds: float,
    threads: int = 2,
    midway: Callable[[], None] | None = None,
) -> Outage:
    """Traffic, the fault for `seconds`, then recovery until the outbox is drained. `midway`
    runs once the engine has seen the outage, while the fault still holds."""
    start, before = utc_now(), version_logs(stack)
    load = make_load(stack, seconds, threads)
    load.start()
    try:
        with fault():
            opened, during = wait_logs(stack, state_is("open"), STATE_TIMEOUT)
            report.add(opened, f"the engine reports its store open during the fault ({during.get('state')})")
            if midway:
                midway()
            time.sleep(seconds)
            during = version_logs(stack)
        recovering = time.monotonic()
        recovered, after = wait_logs(stack, drained, REPLAY_TIMEOUT)
        replay = time.monotonic() - recovering
    finally:
        result = load.stop()
    return Outage(start, before, during, after, recovered, replay, result)


def assert_spooled_and_replayed(report: Report, outage: Outage) -> None:
    report.add(
        outbox_rows(outage.during) > 0, f"the outbox fills during the outage ({outage.during.get('outbox')})"
    )
    report.add(
        outage.recovered,
        f"the store is ok and the outbox drained {outage.replay_seconds:.0f}s after recovery "
        f"(#23 replay time; {outage.after.get('outbox')})",
    )
    dropped = outage.after.get("entriesDropped", 0) - outage.before.get("entriesDropped", 0)
    report.add(dropped == 0, f"no line dropped across the outage ({dropped})")


def stored_lines(
    stack: Stack, start: str, worker_id: str, expect: Callable[[Accounting], bool]
) -> Accounting:
    """Polled until `expect` holds: the engine hands a push on to the store a moment later."""
    seen = account([])

    def check() -> bool:
        nonlocal seen
        seen = account(worker_records(stack, start, utc_now(), worker_id))
        return expect(seen)

    wait_for(check, STORED_TIMEOUT)
    return seen


def burst_ran(report: Report, outcome: BurstOutcome) -> bool:
    summary, run = outcome.summary, outcome.run
    ok = summary is not None and summary.exit_code == 0 and run is not None and run.succeeded
    return report.add(ok, f"loadgen ran its burst and stopped cleanly ({outcome.describe()})")


def assert_burst_delivered(report: Report, stack: Stack, start: str, outcome: BurstOutcome) -> None:
    """Every burst line in the store exactly once: the engine took them all, whatever its store did."""
    if not burst_ran(report, outcome):
        return
    summary = outcome.summary
    lines = stored_lines(stack, start, summary.worker_id, lambda seen: seen.burst_received >= summary.sent)
    whole = lines.burst_received == summary.sent and lines.missing == 0 and lines.duplicates == 0
    report.add(
        whole and summary.dropped == 0 and summary.otherwise_lost == 0,
        f"all {summary.sent} burst lines stored once, none dropped ({summary.dropped}) or otherwise lost "
        f"({summary.otherwise_lost}) by the SDK ({lines})",
    )


def assert_load_readable(report: Report, stack: Stack, outage: Outage) -> None:
    if outage.load.burst:
        assert_burst_delivered(report, stack, outage.start, outage.load.burst)
    else:
        assert_runs_readable(report, stack, outage.load)


def assert_runs_readable(report: Report, stack: Stack, load: LoadResult) -> None:
    """The runs of the outage, read back once replayed: their lines were spooled, not lost."""
    sample = load.completed[-READABLE_SAMPLE:]
    report.add(
        bool(sample), f"runs completed under the fault ({len(load.completed)}, {len(load.errors)} errors)"
    )
    for run in sample:
        lines, _ = workflows.run_lines(stack.engine, run.execution_id)
        report.add(not lines.problems(), f"run {run.execution_id} is readable after the replay ({lines})")


def assert_series(
    report: Report,
    stack: Stack,
    start: str,
    holes_in: tuple[str, ...] = OUTAGE_HOLE_SERVICES,
    accounted_elsewhere: str | None = None,
) -> None:
    """Fails on a duplicate anywhere and on a hole in `holes_in`; the other holes are printed.
    The process `accounted_elsewhere` is left out: another check holds its series."""
    records = [r for r in store_records(stack, start, utc_now()) if r.get("process") != accounted_elsewhere]
    checked = logrecords.seq_problems(records, holes_in)
    report.add(
        not checked,
        f"no duplicate seq, and no hole in {', '.join(holes_in) or 'no series'}, "
        f"in {len(records)} stored records ({checked[:4]})",
    )
    unchecked = [p for p in logrecords.seq_problems(records) if p not in checked]
    if unchecked:
        print(f"report only: seq holes outside {holes_in}: {unchecked[:8]}", flush=True)


def step_store_down_under_load(report: Report, stack: Stack) -> None:
    """C2."""
    outage = outage_under_load(report, stack, lambda: store_stopped(stack), OUTAGE_SECONDS)
    assert_spooled_and_replayed(report, outage)
    assert_load_readable(report, stack, outage)
    assert_series(report, stack, outage.start)


def step_store_down_idle(report: Report, stack: Stack) -> None:
    """C3: with nothing to push, the open circuit attempts nothing and deletes nothing."""
    with store_stopped(stack):
        opened, _ = wait_logs(stack, state_is("open"), STATE_TIMEOUT)
        report.add(opened, "the engine reports its store open")
        attempts_before, rows_before = outbox_attempts_and_rows(stack)
        time.sleep(OUTAGE_SECONDS)
        attempts_after, rows_after = outbox_attempts_and_rows(stack)
    report.add(
        attempts_after == attempts_before == 0 and rows_after >= rows_before,
        f"no replay attempt and no row deleted while open (attempts {attempts_before}->{attempts_after}, "
        f"rows {rows_before}->{rows_after})",
    )
    recovered, after = wait_logs(stack, drained, REPLAY_TIMEOUT)
    report.add(recovered, f"the outbox drains once the store is back ({after.get('outbox')})")


def confirm_misconfigured_target(report: Report, stack: Stack) -> bool:
    status, body = inside.from_engine(stack.clone, "POST", MISCONFIGURED_URL + VICTORIALOGS_PUSH, "{}")
    return report.add(
        status == 404, f"{MISCONFIGURED_URL}{VICTORIALOGS_PUSH} answers 404 ({status}: {body[:80]})"
    )


def step_misconfigured_endpoint(report: Report, stack: Stack) -> None:
    """C4: a store that answers 404 refuses the engine's settings; doctor fails, nothing is lost."""
    if not confirm_misconfigured_target(report, stack):
        return
    with engine_env(report, stack, {"NEOPS_LOG_URL": MISCONFIGURED_URL}) as injected:
        if not injected:
            return
        refused, logs = wait_logs(stack, state_is("misconfigured"), STATE_TIMEOUT)
        report.add(
            refused,
            f"the engine reports its store misconfigured ({logs.get('state')}: {logs.get('lastError')})",
        )
        failing = [name for name, _ in failing_probes(stack.clone)]
        report.add(LOGS_PROBE in failing, f"doctor fails on {LOGS_PROBE!r} ({failing})")
        workflows.run_once(stack.engine, workflows.prepare(stack.deployment, stack.engine))
        held = outbox_rows(version_logs(stack))
        report.add(held > 0, f"the outbox holds the lines the store refused ({held} rows)")
    recovered, after = wait_logs(stack, drained, REPLAY_TIMEOUT)
    report.add(recovered, f"the held lines replay once the URL is right again ({after.get('outbox')})")


def step_unreachable_host(report: Report, stack: Stack) -> None:
    """C4a: an address nobody answers is transient, `open`, and doctor only warns."""
    with engine_env(report, stack, {"NEOPS_LOG_URL": BLACKHOLE_URL}) as injected:
        if not injected:
            return
        opened, logs = wait_logs(stack, state_is("open"), STATE_TIMEOUT)
        report.add(
            opened, f"the engine reports an unreachable store open, not misconfigured ({logs.get('state')})"
        )
        ok, detail = doctor_green(stack.clone)
        report.add(ok, f"doctor passes with the store unreachable, a warning only ({detail})")
    recovered, _ = wait_logs(stack, drained, REPLAY_TIMEOUT)
    report.add(recovered, "the store is ok again once the URL is restored")


def full_up(report: Report, stack: Stack) -> None:
    started = time.monotonic()
    code = neops(stack.clone, "up", "--connect", CONNECT, "--insecure", check=False).returncode
    report.add(
        code == 0, f"`./neops up` with the store unreachable exits 0 [{time.monotonic() - started:.0f}s]"
    )


def step_unreachable_at_boot(report: Report, stack: Stack) -> None:
    """C5: the store is no start dependency of the engine, so `up` is not held up by it."""
    with engine_env(report, stack, {"NEOPS_LOG_URL": BLACKHOLE_URL}, apply=lambda: full_up(report, stack)):
        report.add(
            stack.health_of("engine") == "healthy", f"the engine is healthy ({stack.health_of('engine')})"
        )
        opened, logs = wait_logs(stack, state_is("open"), STATE_TIMEOUT)
        report.add(opened, f"the engine reports the store open ({logs.get('state')})")
    recovered, _ = wait_logs(stack, drained, REPLAY_TIMEOUT)
    report.add(recovered, "the store is ok again once the URL is restored")


def step_partition(report: Report, stack: Stack) -> None:
    """C6: the engine cut off the logs network, under load."""
    outage = outage_under_load(report, stack, lambda: engine_partitioned(stack), PARTITION_SECONDS)
    assert_spooled_and_replayed(report, outage)
    assert_series(report, stack, outage.start)


def carried_rows(
    report: Report, stack: Stack, interrupt: Callable[[], None], what: str
) -> Callable[[], None]:
    """Midway through an outage: interrupt the engine and require the outbox rows to survive."""

    def midway() -> None:
        time.sleep(OUTAGE_SECONDS / 4)
        _, rows_before = outbox_attempts_and_rows(stack)
        interrupt()
        healthy = wait_healthy(stack, "engine")
        _, rows_after = outbox_attempts_and_rows(stack)
        report.add(
            healthy and rows_after >= rows_before,
            f"the outbox rows survive {what} ({rows_before} -> {rows_after}, "
            f"engine {stack.health_of('engine')})",
        )

    return midway


def step_engine_restart_mid_outage(report: Report, stack: Stack) -> None:
    """C7: a graceful restart flushes its queue to the outbox, and the replay picks up after it."""
    midway = carried_rows(report, stack, lambda: compose(stack, "restart", "engine"), "a restart")
    outage = outage_under_load(report, stack, lambda: store_stopped(stack), OUTAGE_SECONDS, midway=midway)
    assert_spooled_and_replayed(report, outage)
    burst = outage.load.burst
    if burst and burst_ran(report, burst):
        # The restarting engine takes no pushes, so loadgen can overflow the SDK's buffer: its
        # worker's holes are held against the drops the SDK counted, not against none.
        assert_drops_accounted(report, stack, outage.start, burst)
        assert_series(report, stack, outage.start, accounted_elsewhere=burst.worker_id)
    else:
        assert_series(report, stack, outage.start)


def kill_engine(stack: Stack) -> None:
    compose(stack, "kill", "-s", "KILL", "engine")
    recreate(stack, "engine")


def step_engine_kill_mid_outage(report: Report, stack: Stack) -> None:
    """C7b: SIGKILL loses the in-memory tail by design, worker lines it had accepted included;
    what reached the outbox survives."""
    midway = carried_rows(report, stack, lambda: kill_engine(stack), "a SIGKILL")
    outage = outage_under_load(report, stack, lambda: store_stopped(stack), OUTAGE_SECONDS, midway=midway)
    report.add(outage.recovered, f"the outbox drains after the kill ({outage.after.get('outbox')})")
    assert_series(report, stack, outage.start, holes_in=())


def step_outbox_cap(report: Report, stack: Stack) -> None:
    """C8: a full outbox deletes its oldest rows and says so once the circuit closes."""
    since = utc_now()
    with engine_env(report, stack, {"NEOPS_LOG_OUTBOX_MAX_ROWS": OUTBOX_CAP}) as injected:
        if not injected:
            return
        outage = outage_under_load(
            report, stack, lambda: store_stopped(stack), OUTAGE_SECONDS, threads=OUTBOX_CAP_THREADS
        )
        outbox = outage.after.get("outbox") or {}
        report.add(outbox.get("dropped", 0) > 0, f"the capped outbox dropped its oldest rows ({outbox})")
        warned = OUTBOX_DROP_MARKER in stack.logs_since("engine", since).lower()
        report.add(
            warned, f"the engine logs the drop once the circuit closes (looked for {OUTBOX_DROP_MARKER!r})"
        )
        assert_newest_kept(report, stack, outage)


def assert_newest_kept(report: Report, stack: Stack, outage: Outage) -> None:
    if outage.load.burst:
        assert_newest_burst_line_kept(report, stack, outage.start, outage.load.burst)
        return
    newest = outage.load.completed[-1:]
    report.add(bool(newest), "a run completed at the end of the outage")
    for run in newest:
        lines, _ = workflows.run_lines(stack.engine, run.execution_id)
        report.add(not lines.problems(), f"the newest run's lines were kept ({lines})")


def assert_newest_burst_line_kept(report: Report, stack: Stack, start: str, outcome: BurstOutcome) -> None:
    if not burst_ran(report, outcome):
        return
    last = outcome.summary.sent - 1
    lines = stored_lines(stack, start, outcome.summary.worker_id, lambda seen: seen.newest_burst == last)
    report.add(lines.newest_burst == last, f"the newest burst line, {last}, was kept ({lines})")


def outage_push_body(timestamp: str) -> str:
    """One valid worker line: validation passes, so only the outage can refuse the push."""
    entry = {"seq": 0, "timestamp": timestamp, "level": "info", "message": "chaos c10 push"}
    return json.dumps({"entries": [entry]})


def push_during_outage(stack: Stack) -> tuple[int | None, str]:
    """To an unknown worker id: the engine must look it up in its database, and with no database
    answers 503 instead of the 404 it gives once the database is back."""
    url = f"{inside.ENGINE_INTERNAL}/workers/{uuid.uuid4()}/logs"
    return inside.from_engine(stack.clone, "POST", url, outage_push_body(utc_now()))


def step_database_and_store_down(report: Report, stack: Stack) -> None:
    """C10: with its database and the store gone, the engine answers pushes 503 and stays up.
    The engine's answer is read directly; under loadgen, the SDK's retry of it is asserted too."""
    load = make_load(stack, DOUBLE_OUTAGE_SECONDS)
    load.start()
    try:
        compose(stack, "stop", "postgres-engine", stack.store.service)
        try:
            time.sleep(DOUBLE_OUTAGE_SECONDS)
            state = (stack.row("engine") or {}).get("State")
            report.add(
                state == "running", f"the engine keeps running without its database and store ({state})"
            )
            status, body = push_during_outage(stack)
            report.add(status == 503, f"the engine answers a worker push 503 ({status}: {body[:120]})")
        finally:
            compose(stack, "start", "postgres-engine", stack.store.service)
    finally:
        result = load.stop()
    report.add(
        wait_healthy(stack, "engine"),
        f"the engine is healthy again ({stack.health_of('engine')}; "
        f"{len(result.runs)} runs, {len(result.errors)} errors under the outage)",
    )
    recovered, _ = wait_logs(stack, drained, REPLAY_TIMEOUT)
    report.add(recovered, "the store is ok and the outbox drained")
    if result.burst:
        assert_sdk_retried(report, result.burst)


def assert_sdk_retried(report: Report, outcome: BurstOutcome) -> None:
    statuses = outcome.summary.retry_statuses if outcome.summary else []
    report.add(
        RETRIED_STATUS in statuses,
        f"the worker SDK retried a push the engine answered {RETRIED_STATUS} (statuses retried: {statuses})",
    )


def parse_worker_exit(inspect: str) -> tuple[bool, str]:
    """`docker inspect` of status, exit code and restart count: refused means it exited
    non-zero or was restarted."""
    status, code, restarts = inspect.split()
    refused = int(code) != 0 or int(restarts) > 0
    return refused, f"{status}, exit {code}, {restarts} restarts"


def worker_refused(stack: Stack) -> tuple[bool, str]:
    name = stack.container("worker")
    out = docker("inspect", "-f", "{{.State.Status}} {{.State.ExitCode}} {{.RestartCount}}", name).stdout
    return parse_worker_exit(out)


def pinned_engine_tag(compose_yaml: str) -> str:
    match = ENGINE_PIN.search(compose_yaml)
    if match is None:
        raise ValueError("compose.yaml pins no neops-workflow-engine tag")
    return match.group(1)


def engine_gates_old_clients(version: str, pinned_tag: str) -> bool:
    line = ".".join(pinned_tag.split(".")[:2]) + "."
    return version.removeprefix("v").startswith(line)


def step_old_worker(report: Report, stack: Stack) -> None:
    """C12a: a 0.2 worker against the 0.44 engine is told it is incompatible and exits."""
    version = workflows.engine_version(stack.deployment)
    pinned = pinned_engine_tag((stack.clone / "compose.yaml").read_text())
    if not engine_gates_old_clients(version, pinned):
        report.add(
            False,
            f"precondition: the engine reports {version!r}, not the pinned {pinned}; "
            f"build it with --build-arg NEOPS_VERSION=v{pinned}",
        )
        return
    since = utc_now()
    try:
        with env_values(stack, {"NEOPS_WORKER_IMAGE": OLD_WORKER_IMAGE}):
            recreate(stack, "worker")
            refused = wait_for(lambda: worker_refused(stack)[0], WORKER_EXIT_TIMEOUT)
            report.add(refused, f"worker {OLD_WORKER_IMAGE} exits ({worker_refused(stack)[1]})")
            log = stack.logs_since("worker", since).lower()
            report.add(REFUSAL_MARKER in log, f"its log names the version refusal ({REFUSAL_MARKER!r})")
    finally:
        recreate(stack, "worker")
    running = wait_for(lambda: stack.health_of("worker") == "running", WORKER_EXIT_TIMEOUT)
    report.add(running, f"the current worker runs again ({stack.health_of('worker')})")


def step_rollback_during_outage(report: Report, stack: Stack) -> None:
    """C14: engine 0.43 boots on the 0.44 schema while the store hangs, leaves log_outbox
    alone, and the re-upgraded engine replays it once the store answers again."""
    workflows.run_once(stack.engine, workflows.prepare(stack.deployment, stack.engine))
    with store_paused(stack):
        with env_values(stack, {"NEOPS_ENGINE_IMAGE": OLD_ENGINE_IMAGE}):
            booted = recreate(stack, "engine")
            report.add(
                booted, f"engine {OLD_ENGINE_IMAGE} boots on the 0.44 schema ({stack.health_of('engine')})"
            )
            _, rows_first = outbox_attempts_and_rows(stack)
            time.sleep(DOUBLE_OUTAGE_SECONDS)
            _, rows_then = outbox_attempts_and_rows(stack)
            report.add(
                rows_first == rows_then,
                f"the old engine leaves log_outbox alone ({rows_first} -> {rows_then})",
            )
        report.add(recreate(stack, "engine"), "the 0.44 engine is back and healthy")
    recovered, after = wait_logs(stack, drained, REPLAY_TIMEOUT)
    report.add(
        recovered and rows_then > 0,
        f"the kept rows replay after the re-upgrade ({rows_then} rows; {after.get('outbox')})",
    )


def step_celery_child_restart(report: Report, stack: Stack) -> None:
    """C15, report only: core lines in flight in a Celery child are lost when it restarts."""
    if stack.health_of("cms-worker") in ("absent", "exited"):
        print("C15 (report only): no cms-worker in this deployment, nothing to restart", flush=True)
        return
    target = workflows.prepare(stack.deployment, stack.engine)
    execution_id = workflows.execute(stack.engine, target)
    killed = compose(
        stack, "exec", "-T", "cms-worker", "pkill", "-f", "celery.*worker", check=False
    ).returncode
    status = workflows.wait_settled(stack.engine, execution_id)
    lines, _ = workflows.run_lines(stack.engine, execution_id, timeout=10)
    print(f"C15 (report only): pkill exit {killed}; run {execution_id} {status}; {lines}", flush=True)


def run_durations(stack: Stack, target: workflows.Target, count: int) -> list[float]:
    runs = [workflows.run_once(stack.engine, target, interval=FINE_POLL) for _ in range(count)]
    return [run.seconds for run in runs]


def step_slow_store_at_unlock(report: Report, stack: Stack) -> None:
    """C16: core's push to a store that hangs must not hold the unlock up. A run's duration
    covers its unlock, so every run with the store paused stays within the baseline's worst."""
    target = workflows.prepare(stack.deployment, stack.engine)
    baseline = max(run_durations(stack, target, SLOW_STORE_RUNS))
    with store_paused(stack):
        paused = time.monotonic()
        slow = run_durations(stack, target, SLOW_STORE_RUNS)
        time.sleep(max(0.0, SLOW_STORE_PAUSE - (time.monotonic() - paused)))
    limit = baseline + SLOW_STORE_SLACK
    report.add(
        max(slow) < limit,
        f"no run slowed by the paused store: worst {max(slow):.1f}s < {limit:.1f}s "
        f"({', '.join(f'{s:.1f}' for s in slow)})",
    )
    recovered, _ = wait_logs(stack, drained, REPLAY_TIMEOUT)
    report.add(recovered, "the store is ok and the outbox drained after the pause")


def step_loadgen_rate(report: Report, stack: Stack) -> None:
    """B8: loadgen sustains its rate for 60 s, and every line it sends reaches the store."""
    start = utc_now()
    load = burst_load(stack, BURST_RATE, LOADGEN_CHECK_SECONDS)
    load.start()
    outcome = load.finish()
    summary = outcome.summary
    if summary:
        report.add(
            summary.sent == round(BURST_RATE * LOADGEN_CHECK_SECONDS)
            and summary.burst_seconds <= LOADGEN_CHECK_SECONDS + RATE_SLACK_SECONDS,
            f"loadgen sent {summary.sent} lines in {summary.burst_seconds}s "
            f"({BURST_RATE}/s for {LOADGEN_CHECK_SECONDS:.0f}s wanted)",
        )
    assert_burst_delivered(report, stack, start, outcome)


def step_engine_down_overflow(report: Report, stack: Stack) -> None:
    """C9: past the SDK's buffer the oldest lines go, the next push says how many, and nothing
    else is lost: by seq, the store holds what the SDK numbered minus what it dropped."""
    start = utc_now()
    load = burst_load(stack, OVERFLOW_RATE, OVERFLOW_SECONDS)
    load.start()
    try:
        with engine_stopped(stack):
            time.sleep(ENGINE_DOWN_SECONDS)
    finally:
        outcome = load.finish()
    if burst_ran(report, outcome):
        assert_overflow_accounted(report, stack, start, outcome)


def assert_overflow_accounted(report: Report, stack: Stack, start: str, outcome: BurstOutcome) -> None:
    summary = outcome.summary
    report.add(
        summary.dropped > 0, f"the SDK dropped N > 0 lines while the engine was down (N={summary.dropped})"
    )
    assert_drops_accounted(report, stack, start, outcome)


def assert_drops_accounted(report: Report, stack: Stack, start: str, outcome: BurstOutcome) -> None:
    """By seq, the store holds what the SDK numbered minus what it dropped, and the stored
    `dropped N log lines` lines tell the engine exactly that N."""
    summary = outcome.summary
    report.add(
        summary.otherwise_lost == 0,
        f"no line lost but to the overflow ({summary.unpushed} unpushed, {summary.refused} refused, "
        f"{summary.lease_refused} lease refusals)",
    )
    last = summary.sent - 1
    lines = stored_lines(
        stack,
        start,
        summary.worker_id,
        lambda seen: (
            seen.newest_burst == last
            and seen.missing <= summary.dropped
            and seen.drops_reported >= summary.dropped
        ),
    )
    report.add(lines.duplicates == 0, f"no line stored twice ({lines.duplicates})")
    report.add(
        lines.received == lines.assigned - summary.dropped,
        f"received {lines.received} = sent {lines.assigned} - N {summary.dropped}, by seq "
        f"({lines.burst_received} of {summary.sent} burst lines)",
    )
    report.add(
        lines.drops_reported == summary.dropped,
        f"stored dropped-lines lines report {lines.drops_reported} of the {summary.dropped} the SDK dropped",
    )


def step_worker_kill_mid_push(report: Report, stack: Stack) -> None:
    """C11: a SIGKILLed worker leaves whole batches behind, never part of one or one twice, and
    a fresh worker takes its place."""
    start = utc_now()
    load = burst_load(stack, BURST_RATE, KILL_BURST_SECONDS)
    load.start()
    time.sleep(KILL_AFTER_SECONDS)
    load.kill()
    killed_at = time.monotonic()
    outcome = load.finish()
    report.add(
        outcome.summary is None,
        f"loadgen's worker {outcome.worker_id} died mid-burst (exit {outcome.exit_code})",
    )
    assert_fresh_worker_online(report, stack, killed_at)
    records = worker_records(stack, start, utc_now(), outcome.worker_id)
    problems = logrecords.seq_problems(records, (logrecords.WORKER_SERVICE,))
    report.add(
        bool(records) and not problems,
        f"no partial or duplicate batch in the killed worker's {len(records)} stored lines ({problems[:4]})",
    )


def assert_fresh_worker_online(report: Report, stack: Stack, killed_at: float) -> None:
    """A new loadgen worker, started with --no-deps, is ONLINE within a minute of the kill and runs a job."""
    fresh = burst_load(stack, FRESH_RATE, FRESH_SECONDS)
    try:
        worker_id = fresh.launch()
        left = max(0.0, ONLINE_TIMEOUT - (time.monotonic() - killed_at))
        online = wait_for(lambda: worker_state(stack, worker_id) == workflows.ONLINE, left, interval=2.0)
        report.add(
            online,
            f"a fresh worker {worker_id} is ONLINE {time.monotonic() - killed_at:.0f}s after the kill "
            f"({worker_state(stack, worker_id)})",
        )
        fresh.trigger()
    except Exception:
        fresh.kill()
        raise
    burst_ran(report, fresh.finish())


def worker_state(stack: Stack, worker_id: str) -> str | None:
    _, states = workflows.workers(stack.engine)
    return states.get(worker_id)


# They drive loadgen.py whatever --load says.
LOADGEN_STEPS = ("loadgen", "c9", "c11")
LOG_STEPS: dict[str, Callable[[Report, Stack], None]] = {
    "loadgen": step_loadgen_rate,
    "c2": step_store_down_under_load,
    "c3": step_store_down_idle,
    "c4": step_misconfigured_endpoint,
    "c4a": step_unreachable_host,
    "c5": step_unreachable_at_boot,
    "c6": step_partition,
    "c7": step_engine_restart_mid_outage,
    "c7b": step_engine_kill_mid_outage,
    "c8": step_outbox_cap,
    "c9": step_engine_down_overflow,
    "c10": step_database_and_store_down,
    "c11": step_worker_kill_mid_push,
    "c12a": step_old_worker,
    "c15": step_celery_child_restart,
    "c16": step_slow_store_at_unlock,
    # Last: it swaps the engine image and back, the riskiest thing the suite does to a clone.
    "c14": step_rollback_during_outage,
}
