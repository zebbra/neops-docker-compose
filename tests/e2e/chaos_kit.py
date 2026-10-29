"""What the chaos steps share: the stack under test, fault injection and the waits that follow.

Injection rules (the logging test plan, §5):
- An engine environment fault goes into the clone's compose.override.yaml, the last file in
  COMPOSE_FILE, so it beats compose.logs.yaml; .env cannot (the overlay hardcodes the URL) and
  neither can data/secrets/engine.env (`environment:` beats `env_file:`). The engine is then
  recreated alone, and every injected value is read back with printenv before the step runs.
- Recovery restarts single services with --no-deps, never a bare `./neops up`, which would
  restart a store the step means to keep down.
- Assertions read GET /version, where doctor would only WARN about an unreachable store.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

import inside
import logrecords
import workflows
import yaml
from loadgen_driver import BurstLoad, BurstOutcome
from run_scenario import CONNECT, Report, neops

from neops_compose.compose import Compose
from neops_compose.doctor import NAME_WIDTH, Http, Login, login
from neops_compose.scenario import OVERRIDE_FILE, Scenario
from neops_compose.urls import PublicUrl

WAIT_TIMEOUT = "600"
HEALTHY_TIMEOUT = 300.0
# Core allows five logins a minute from one address and doctor spends two of them, so a
# doctor run right after another can fail on the rate limit alone. That is not a recovery
# failure: wait out the window and ask again.
RATE_LIMIT_MARKERS = ("rate limit", "too many")
OUTBOX_SQL = "select coalesce(max(attempts), 0), count(*) from log_outbox"
STORE_QUERY_SERVICES = (logrecords.ENGINE_SERVICE, logrecords.WORKER_SERVICE)
SERIES_FIELDS = ("service", "process", "seq", "job_id")
# VictoriaLogs keeps the line under its own name; the records come back with the engine's.
STORE_MESSAGE_FIELD = "_msg"
LOADGEN_LOAD = "loadgen"
WORKFLOW_LOAD = "workflows"
LOADS = (LOADGEN_LOAD, WORKFLOW_LOAD)
# The plan's C2 rate, for every step "under load".
BURST_RATE = 300
# The burst outlasts the fault by this much: the fault starts once the lines flow, and the
# store takes a few seconds to be seen open.
LOAD_LEAD_SECONDS = 30.0
ENGINE_HEALTH_URL = inside.ENGINE_INTERNAL + inside.ENGINE_HEALTH
ANSWER_POLL = 0.5


@dataclass(frozen=True)
class Stack:
    """One running deployment: the clone that owns it and the credentials to talk to it."""

    clone: Path
    values: dict[str, str]
    http: Http
    load: str = LOADGEN_LOAD

    @property
    def cms(self) -> PublicUrl:
        return PublicUrl.parse(self.values["NEOPS_CMS_URL"])

    @property
    def project(self) -> str:
        return self.values["COMPOSE_PROJECT_NAME"]

    @property
    def scenario(self) -> Scenario:
        return Scenario.from_env(self.values)

    @property
    def store(self) -> logrecords.LogStore | None:
        return logrecords.store_of(self.values)

    @property
    def engine_env(self) -> Path:
        return self.clone / "data" / "secrets" / "engine.env"

    @property
    def env_file(self) -> Path:
        return self.clone / ".env"

    @property
    def override_file(self) -> Path:
        return self.clone / OVERRIDE_FILE

    @property
    def state_file(self) -> Path:
        return self.clone / "data" / ".neops" / "state.json"

    @property
    def deployment(self) -> workflows.Deployment:
        return workflows.Deployment(self.clone, self.values, self.http)

    @cached_property
    def engine(self) -> workflows.Engine:
        """Minted on first use; the CMS issues it, so no login is spent."""
        return workflows.Engine(self.deployment, lambda: workflows.admin_token(self.deployment))

    def admin_login(self) -> Login:
        """One retry: a doctor run just before this one can have spent the login budget."""
        for _ in range(2):
            result = login(
                self.cms,
                self.http,
                self.values.get("NEOPS_ADMIN_USER", "neops"),
                self.values["NEOPS_ADMIN_PASSWORD"],
            )
            if result.token or not result.rate_limited:
                return result
            print("login rate limited; waiting 60s", flush=True)
            time.sleep(60)
        return Login(error="rate limited twice")

    def api_keys(self) -> int:
        return len(json.loads(self.state_file.read_text())["api_keys"])

    def last_up_images(self) -> list[str]:
        """What the previous `up` ran, which is what the next one has to find or fetch again."""
        return sorted(set(json.loads(self.state_file.read_text())["last_up"]["images"].values()))

    def logs_since(self, service: str, since: str) -> str:
        """The engine's /workers needs a worker:read role the admin user may lack, so the
        worker's own log is what says whether it is still polling."""
        out = compose(self, "logs", "--no-log-prefix", "--since", since, service, capture=True)
        return out.stdout + out.stderr

    def row(self, service: str) -> dict | None:
        return next((r for r in Compose(self.clone).ps() if r.get("Service") == service), None)

    def health_of(self, service: str) -> str:
        row = self.row(service)
        if row is None:
            return "absent"
        return row.get("Health") or row.get("State", "")

    def is_up(self, service: str) -> bool:
        """`running` for a service without a healthcheck, `healthy` for one with."""
        return self.health_of(service) in ("running", "healthy")

    def container(self, service: str) -> str:
        row = self.row(service)
        if row is None:
            raise RuntimeError(f"no {service} container")
        return row["Name"]


def load_stack(clone: Path, load: str = LOADGEN_LOAD) -> Stack:
    values = {}
    for line in (clone / ".env").read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return Stack(clone, values, Http(connect=CONNECT, insecure=True), load)


def compose(
    stack: Stack, *args: str, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess:
    return neops(stack.clone, "compose", "--", *args, check=check, capture=capture)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    print("+ docker " + " ".join(args), flush=True)
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=True)


def failing_probes(clone: Path) -> list[tuple[str, str]]:
    """doctor prints `FAIL <name padded to NAME_WIDTH> <detail>`."""
    out = neops(clone, "doctor", "--connect", CONNECT, "--insecure", check=False, capture=True)
    print(out.stdout + out.stderr, flush=True)
    start = len("FAIL ")
    return [
        (line[start : start + NAME_WIDTH].strip(), line[start + NAME_WIDTH :].strip())
        for line in out.stdout.splitlines()
        if line.startswith("FAIL")
    ]


def _rate_limited(detail: str) -> bool:
    return any(marker in detail.lower() for marker in RATE_LIMIT_MARKERS)


def doctor_green(clone: Path, tries: int = 3) -> tuple[bool, str]:
    failures: list[tuple[str, str]] = []
    for _ in range(tries):
        failures = failing_probes(clone)
        if not failures:
            return True, ""
        if not all(_rate_limited(detail) for _, detail in failures):
            break
        print("doctor hit core's login rate limit; waiting 60s", flush=True)
        time.sleep(60)
    return False, "; ".join(f"{name}: {detail}" for name, detail in failures)


def utc_now() -> str:
    """`docker compose logs --since` wants an RFC 3339 instant, and the daemon works in UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def wait_for(predicate, timeout: float, interval: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def timed(report: Report, ok: bool, message: str, started: float) -> bool:
    return report.add(ok, f"{message} [{time.monotonic() - started:.0f}s]")


def set_value(env_text: str, key: str, value: str) -> str:
    """Replace KEY's line, or append one when the .env does not have it."""
    lines = env_text.splitlines()
    if not any(line.startswith(f"{key}=") for line in lines):
        lines.append(f"{key}={value}")
    lines = [f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines]
    return "".join(line + "\n" for line in lines)


def _as_mapping(environment: object) -> dict[str, str]:
    """Compose takes `environment` as a mapping or as a list of KEY=VALUE strings."""
    if isinstance(environment, dict):
        return {str(k): str(v) for k, v in environment.items()}
    return dict(item.split("=", 1) for item in environment or [])


def with_engine_env(override_text: str, env: dict[str, str]) -> str:
    """The clone's compose.override.yaml with these engine environment entries set."""
    data = yaml.safe_load(override_text) or {}
    engine = data.setdefault("services", {}).setdefault("engine", {})
    engine["environment"] = _as_mapping(engine.get("environment")) | env
    header = "".join(line for line in override_text.splitlines(keepends=True) if line.startswith("#"))
    return header + yaml.safe_dump(data, sort_keys=False)


def wait_healthy(stack: Stack, service: str, timeout: float = HEALTHY_TIMEOUT) -> bool:
    return wait_for(lambda: stack.health_of(service) == "healthy", timeout)


def start_alone(stack: Stack, service: str) -> None:
    """One service, with whatever configuration the files now say, and nothing it depends on."""
    compose(stack, "up", "-d", "--no-deps", service)


def recreate(stack: Stack, service: str) -> bool:
    start_alone(stack, service)
    return wait_healthy(stack, service) if service == "engine" else True


def printenv(stack: Stack, key: str) -> str:
    try:
        return Compose(stack.clone).exec("engine", "printenv", key).strip()
    except Exception:
        return ""


@contextmanager
def engine_env(report: Report, stack: Stack, env: dict[str, str], apply=None) -> Iterator[bool]:
    """Inject engine environment through compose.override.yaml, verified with printenv; the
    override is restored and the engine recreated again on the way out, whatever happened."""
    original = stack.override_file.read_text()
    stack.override_file.write_text(with_engine_env(original, env))
    try:
        (apply or (lambda: recreate(stack, "engine")))()
        seen = {key: printenv(stack, key) for key in env}
        verified = report.add(seen == env, f"the engine runs with the injected {env} (printenv: {seen})")
        yield verified
    finally:
        stack.override_file.write_text(original)
        recreate(stack, "engine")


@contextmanager
def env_values(stack: Stack, env: dict[str, str]) -> Iterator[None]:
    """Edit .env for the length of a step; it is restored on the way out."""
    original = stack.env_file.read_text()
    text = original
    for key, value in env.items():
        text = set_value(text, key, value)
    stack.env_file.write_text(text)
    try:
        yield
    finally:
        stack.env_file.write_text(original)


def version_logs(stack: Stack) -> dict:
    return workflows.engine_logs_status(stack.deployment)


def outbox_rows(logs: dict) -> int:
    return int((logs.get("outbox") or {}).get("rows", 0))


def wait_logs(stack: Stack, accept, timeout: float, interval: float = 2.0) -> tuple[bool, dict]:
    """Poll /version until `accept(logs)` holds; the last `logs` seen either way."""
    seen: dict = {}

    def check() -> bool:
        nonlocal seen
        try:
            seen = version_logs(stack)
        except Exception as exc:
            seen = {"error": str(exc)}
            return False
        return bool(accept(seen))

    return wait_for(check, timeout, interval), seen


def state_is(state: str):
    return lambda logs: logs.get("state") == state


def drained(logs: dict) -> bool:
    return logs.get("state") == "ok" and outbox_rows(logs) == 0


def engine_sql(stack: Stack, sql: str) -> str:
    return (
        Compose(stack.clone)
        .exec("postgres-engine", "psql", "-U", "postgres", "-d", "neops-workflow", "-tAc", sql)
        .strip()
    )


def outbox_attempts_and_rows(stack: Stack) -> tuple[int, int]:
    attempts, rows = engine_sql(stack, OUTBOX_SQL).split("|")
    return int(attempts), int(rows)


@contextmanager
def store_stopped(stack: Stack) -> Iterator[None]:
    compose(stack, "stop", stack.store.service)
    try:
        yield
    finally:
        compose(stack, "start", stack.store.service)


@contextmanager
def store_paused(stack: Stack) -> Iterator[None]:
    """A store that accepts the connection and never answers: every push runs into its timeout."""
    name = stack.container(stack.store.service)
    docker("pause", name)
    try:
        yield
    finally:
        docker("unpause", name, check=False)


def running_services(stack: Stack) -> set[str]:
    return {row["Service"] for row in Compose(stack.clone).ps() if row.get("State") == "running"}


@dataclass
class FaultSpan:
    """A fault in time.monotonic(): from when it was in place to when the service it took away
    answered again, None until then."""

    began: float = field(default_factory=time.monotonic)
    ended: float | None = None


def engine_answered_at(stack: Stack) -> float | None:
    """Asked from inside its container, so before the healthcheck and the proxy see it: a worker
    reaches the engine directly too."""
    if wait_for(lambda: inside.engine_reaches(stack.clone, ENGINE_HEALTH_URL), HEALTHY_TIMEOUT, ANSWER_POLL):
        return time.monotonic()
    return None


@contextmanager
def engine_stopped(stack: Stack) -> Iterator[FaultSpan]:
    """Back with --no-deps, so nothing the engine depends on is restarted with it. Whatever the
    stop took down with it is started again as it was, not recreated."""
    running = running_services(stack)
    compose(stack, "stop", "engine")
    span = FaultSpan()
    try:
        yield span
    finally:
        start_alone(stack, "engine")
        span.ended = engine_answered_at(stack)
        wait_healthy(stack, "engine")
        stopped = sorted(running - running_services(stack))
        if stopped:
            compose(stack, "start", *stopped)


@contextmanager
def engine_partitioned(stack: Stack) -> Iterator[None]:
    network, name = f"{stack.project}_logs", stack.container("engine")
    docker("network", "disconnect", network, name)
    try:
        yield
    finally:
        docker("network", "connect", network, name, check=False)


class Load(Protocol):
    """Traffic for the steps that run "under load"."""

    def start(self) -> None: ...

    def stop(self) -> LoadResult: ...


@dataclass
class LoadResult:
    runs: list[workflows.Run] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    burst: BurstOutcome | None = None

    @property
    def completed(self) -> list[workflows.Run]:
        return [r for r in self.runs if r.succeeded]


class WorkflowLoad:
    """workflows.py runs back to back on `threads` threads: the fallback (`chaos.py --load
    workflows`) for a worker image without the SDK's push sink, which loadgen needs. It has no
    line rate. A run that fails while the step has the engine down is counted, not raised."""

    def __init__(self, stack: Stack, threads: int = 2):
        self.stack = stack
        self.threads = threads
        self.result = LoadResult()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        target = workflows.prepare(self.stack.deployment, self.stack.engine)
        self._workers = [
            threading.Thread(target=self._loop, args=(target,), daemon=True) for _ in range(self.threads)
        ]
        for worker in self._workers:
            worker.start()

    def _loop(self, target: workflows.Target) -> None:
        while not self._stop.is_set():
            try:
                run = workflows.run_once(self.stack.engine, target)
            except Exception as exc:
                with self._lock:
                    self.result.errors.append(f"{type(exc).__name__}: {exc}"[:200])
                time.sleep(2)
                continue
            with self._lock:
                self.result.runs.append(run)

    def stop(self) -> LoadResult:
        self._stop.set()
        for worker in self._workers:
            worker.join(timeout=workflows.RUN_TIMEOUT + 10)
        return self.result


class LoadgenLoad:
    """One loadgen burst at BURST_RATE: the lines take the worker SDK's real push path."""

    def __init__(self, stack: Stack, seconds: float):
        self.burst = burst_load(stack, BURST_RATE, seconds)

    def start(self) -> None:
        self.burst.start()

    def stop(self) -> LoadResult:
        outcome = self.burst.finish()
        return LoadResult(runs=[outcome.run] if outcome.run else [], errors=outcome.errors, burst=outcome)


def burst_load(stack: Stack, rate: int, seconds: float) -> BurstLoad:
    return BurstLoad(stack.clone, stack.project, stack.engine, rate, seconds)


def make_load(stack: Stack, seconds: float, threads: int = 2) -> Load:
    """The one place a step gets its traffic from. `seconds` is how long the step needs it;
    workflow runs go on until stopped and `threads` only sizes them."""
    if stack.load == WORKFLOW_LOAD:
        return WorkflowLoad(stack, threads)
    return LoadgenLoad(stack, seconds + LOAD_LEAD_SECONDS)


def store_query(start: str, end: str, where: str, fields: tuple[str, ...]) -> str:
    return f"_time:[{start}, {end}] {where} | fields {', '.join(fields)}"


def store_records(stack: Stack, start: str, end: str) -> list[dict]:
    """The engine's and the workers' records of a window, straight from VictoriaLogs: the read
    API serves one run, a job or a worker at a time, and a seq hole shows only in a whole series."""
    services = ",".join(json.dumps(s) for s in STORE_QUERY_SERVICES)
    return query_store(stack, store_query(start, end, f"service:in({services})", SERIES_FIELDS))


def worker_records(stack: Stack, start: str, end: str, worker_id: str) -> list[dict]:
    """One worker's records of a window, its messages included, under the engine's key names."""
    where = f"service:={json.dumps(logrecords.WORKER_SERVICE)} process:={json.dumps(worker_id)}"
    records = query_store(stack, store_query(start, end, where, (*SERIES_FIELDS, STORE_MESSAGE_FIELD)))
    return [as_log_record(record) for record in records]


def as_log_record(stored: dict) -> dict:
    record = {key: value for key, value in stored.items() if key != STORE_MESSAGE_FIELD}
    record["message"] = stored.get(STORE_MESSAGE_FIELD, "")
    return record


def query_store(stack: Stack, query: str) -> list[dict]:
    url = f"{stack.store.url}/select/logsql/query?{urlencode({'query': query})}"
    status, body = inside.from_engine(stack.clone, "GET", url)
    if status != 200:
        raise RuntimeError(f"{url} answered {status}: {body[:200]}")
    return [json.loads(line) for line in body.splitlines() if line.strip()]
