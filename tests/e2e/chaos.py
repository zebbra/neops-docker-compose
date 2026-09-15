#!/usr/bin/env python3
"""Chaos checks against a stack left running by run_scenario.py --keep.

    uv run python tests/e2e/chaos.py <clone dir> [--prune] [--only STEP ...]

kill      Kill each service in turn; `compose up -d --wait` brings it back and doctor is green.
restart   Stop and start the stack; the admin, the device group, the API key and the state survive.
prune     The same, with `docker system prune -a --volumes` in the middle, so `up` re-pulls.
env       Corrupt .env three ways; `./neops check` names each problem and exits 1.
engine    Restart the engine under a polling worker; the worker keeps working.
database  Stop the CMS database; logins fail while it is gone and work again when it returns.

`--only` picks steps by those names and runs them in the listed order; the default is every
step but `prune`, and `--prune` swaps `prune` in for `restart`. The prune removes every unused
image, container and volume on the host, so it asks before running. Exit code 0 only when
every step passed. See tests/e2e/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_scenario import (  # noqa: E402
    CONNECT,
    GROUP_DELETE,
    GROUP_READ,
    GROUP_UPSERT,
    Report,
    gql,
    neops,
)

from neops_compose.compose import Compose  # noqa: E402
from neops_compose.doctor import NAME_WIDTH, Http, Login, login  # noqa: E402
from neops_compose.urls import PublicUrl  # noqa: E402

KILL_SERVICES = ("cms", "engine", "redis", "postgres-cms", "worker", "traefik")
WAIT_TIMEOUT = "600"
POLLING_MARKER = "Worker ready and listening for jobs"
# A database row, which the files under data/ do not prove on their own. run_scenario.py uses
# its own group name and deletes it again, so the two can run against one clone without
# either seeing the other's row.
CHAOS_GROUP = "e2e-chaos-survivor"

# Core allows five logins a minute from one address and doctor spends two of them, so a
# doctor run right after another can fail on the rate limit alone. That is not a recovery
# failure: wait out the window and ask again.
RATE_LIMIT_MARKERS = ("rate limit", "too many")
STEP_NAMES = ("kill", "restart", "prune", "env", "engine", "database")
DEFAULT_STEPS = tuple(name for name in STEP_NAMES if name != "prune")
PRUNE_WARNING = (
    "docker system prune -a --volumes removes every unused image, container and volume on "
    "this host, including other projects' (KIND, the lab)."
)


@dataclass(frozen=True)
class Stack:
    """One running deployment: the clone that owns it and the credentials to talk to it."""

    clone: Path
    values: dict[str, str]
    http: Http

    @property
    def cms(self) -> PublicUrl:
        return PublicUrl.parse(self.values["NEOPS_CMS_URL"])

    @property
    def project(self) -> str:
        return self.values["COMPOSE_PROJECT_NAME"]

    @property
    def engine_env(self) -> Path:
        return self.clone / "data" / "secrets" / "engine.env"

    @property
    def env_file(self) -> Path:
        return self.clone / ".env"

    @property
    def state_file(self) -> Path:
        return self.clone / "data" / ".neops" / "state.json"

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
        """The engine's /workers needs a worker:read role the admin user does not have, so
        the worker's own log is what says whether it is still polling."""
        out = neops(
            self.clone,
            "compose",
            "--",
            "logs",
            "--no-log-prefix",
            "--since",
            since,
            service,
            capture=True,
        )
        return out.stdout + out.stderr

    def health_of(self, service: str) -> str:
        row = next((r for r in Compose(self.clone).ps() if r.get("Service") == service), None)
        if row is None:
            return "absent"
        return row.get("Health") or row.get("State", "")


def load_stack(clone: Path) -> Stack:
    values = {}
    for line in (clone / ".env").read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return Stack(clone, values, Http(connect=CONNECT, insecure=True))


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


def step_kill_and_recover(report: Report, stack: Stack) -> None:
    for service in KILL_SERVICES:
        started = time.monotonic()
        neops(stack.clone, "compose", "--", "kill", service, check=False)
        neops(stack.clone, "compose", "--", "up", "-d", "--wait", "--wait-timeout", WAIT_TIMEOUT)
        ok, detail = doctor_green(stack.clone)
        timed(report, ok, f"doctor green after killing {service} ({detail})", started)


def admin_can_log_in(report: Report, stack: Stack, when: str) -> None:
    result = stack.admin_login()
    report.add(bool(result.token), f"the admin can log in {when} ({result.error})")


def confirm_prune() -> bool:
    print(PRUNE_WARNING, flush=True)
    if os.environ.get("CHAOS_PRUNE") == "yes":
        return True
    if not sys.stdin.isatty():
        print("refusing to prune: not a terminal and CHAOS_PRUNE is not yes", flush=True)
        return False
    return input("type PRUNE to continue: ").strip() == "PRUNE"


def docker_out(*args: str) -> list[str]:
    result = subprocess.run(["docker", *args], check=True, text=True, capture_output=True)
    return result.stdout.split()


def image_exists(reference: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", reference], capture_output=True).returncode == 0


def project_volumes(project: str) -> list[str]:
    return docker_out("volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}")


def prune_host(report: Report) -> bool:
    if not confirm_prune():
        return report.add(False, "prune was requested but not confirmed")
    subprocess.run(["docker", "system", "prune", "-a", "--volumes", "-f"], check=True)
    return True


def assert_prune_emptied_the_host(report: Report, stack: Stack) -> None:
    """The local monitor image is the one the prune must not take: nothing can re-pull a
    local-only tag, so a running container elsewhere on the host has to pin it."""
    local = stack.values["NEOPS_MONITOR_IMAGE"]
    fetchable = [ref for ref in stack.last_up_images() if ref != local]
    left = [ref for ref in fetchable if image_exists(ref)]
    report.add(not left, f"the prune removed all {len(fetchable)} fetchable stack images ({left})")
    report.add(image_exists(local), f"{local} survived the prune")
    volumes = project_volumes(stack.project)
    report.add(not volumes, f"no docker volume belongs to {stack.project} ({volumes})")


def bring_up(report: Report, stack: Stack, what: str) -> None:
    started = time.monotonic()
    neops(stack.clone, "up", "--connect", CONNECT, "--insecure")
    report.add(True, f"`up` brought the stack back {what} [{time.monotonic() - started:.0f}s]")


def write_group(report: Report, stack: Stack, token: str) -> str | None:
    try:
        upserted = gql(stack.http, stack.cms, token, GROUP_UPSERT, {"n": CHAOS_GROUP})
        group_id = upserted["deviceGroupUpsert"]["deviceGroup"]["id"]
    except Exception as exc:
        report.add(False, f"the admin creates the device group {CHAOS_GROUP!r} ({exc})")
        return None
    report.add(True, f"the admin creates the device group {CHAOS_GROUP!r} (id {group_id})")
    return group_id


def assert_group_survived(report: Report, stack: Stack, token: str, group_id: str, what: str) -> None:
    """Read the row back and delete it again: the read proves the data survived, the delete
    proves the database came back writable and leaves the clone as it was found."""
    try:
        found = gql(stack.http, stack.cms, token, GROUP_READ, {"n": CHAOS_GROUP})["groups"]["results"]
        survived = [g["id"] for g in found] == [group_id]
        report.add(survived, f"the device group is still there {what} ({found})")
        gql(stack.http, stack.cms, token, GROUP_DELETE, {"id": group_id})
        left = gql(stack.http, stack.cms, token, GROUP_READ, {"n": CHAOS_GROUP})["groups"]["results"]
        report.add(left == [], f"the admin can write again {what}: the group deletes ({left})")
    except Exception as exc:
        report.add(False, f"the device group is still there {what} ({exc})")


def step_restart_survival(report: Report, stack: Stack, prune: bool) -> None:
    started = time.monotonic()
    before = stack.admin_login()
    report.add(bool(before.token), f"the admin can log in before the restart ({before.error})")
    group_id = write_group(report, stack, before.token) if before.token else None
    engine_env_before = stack.engine_env.read_bytes()
    keys_before = stack.api_keys()

    neops(stack.clone, "down")
    if prune and not prune_host(report):
        return
    what = "after a prune and a restart" if prune else "after a stop and a start"
    if prune:
        assert_prune_emptied_the_host(report, stack)
    bring_up(report, stack, what)

    after = stack.admin_login()
    report.add(bool(after.token), f"the admin can log in {what} ({after.error})")
    if after.token and group_id:
        assert_group_survived(report, stack, after.token, group_id, what)
    report.add(
        stack.engine_env.read_bytes() == engine_env_before, f"data/secrets/engine.env is unchanged {what}"
    )
    keys_after = stack.api_keys()
    timed(
        report,
        keys_before == keys_after == 1,
        f"one API key before and after ({keys_before} -> {keys_after})",
        started,
    )
    ok, detail = doctor_green(stack.clone)
    report.add(ok, f"doctor green {what} ({detail})")


def broken_envs(original: str) -> list[tuple[str, str, str]]:
    """Each entry is (label, the corrupted .env, the words `check` must say about it)."""
    return [
        (
            "a placeholder admin password",
            set_value(original, "NEOPS_ADMIN_PASSWORD", "changeme"),
            "placeholder",
        ),
        ("an ftp:// CMS URL", set_value(original, "NEOPS_CMS_URL", "ftp://cms.neops.localhost"), "scheme"),
        ("both proxy overlays", add_overlay(original, "compose.expose.yaml"), "not both"),
    ]


def set_value(env_text: str, key: str, value: str) -> str:
    lines = [f"{key}={value}" if line.startswith(f"{key}=") else line for line in env_text.splitlines()]
    return "".join(line + "\n" for line in lines)


def add_overlay(env_text: str, overlay: str) -> str:
    files = next(line.split("=", 1)[1] for line in env_text.splitlines() if line.startswith("COMPOSE_FILE="))
    return set_value(env_text, "COMPOSE_FILE", files + ":" + overlay)


def step_broken_env(report: Report, stack: Stack) -> None:
    original = stack.env_file.read_text()
    try:
        for label, broken, needle in broken_envs(original):
            started = time.monotonic()
            stack.env_file.write_text(broken)
            result = neops(stack.clone, "check", "--no-images", check=False, capture=True)
            output = result.stdout + result.stderr
            print(output, flush=True)
            blocked = result.returncode == 1 and needle in output
            timed(report, blocked, f"check blocks {label} (wanted {needle!r})", started)
    finally:
        stack.env_file.write_text(original)
    restored = neops(stack.clone, "check", "--no-images", check=False)
    report.add(restored.returncode == 0, "check passes again once .env is restored")


def worker_verdict(stack: Stack, since: str) -> str:
    """A worker that never noticed the restart logs nothing: the engine keeps its worker
    registrations in Postgres, so an engine restart costs no re-registration."""
    if POLLING_MARKER in stack.logs_since("worker", since):
        return "it restarted and registered again"
    return "it kept polling, with no re-registration to do"


def step_engine_restart(report: Report, stack: Stack) -> None:
    started = time.monotonic()
    since = utc_now()
    neops(stack.clone, "restart", "engine")
    healthy = wait_for(lambda: stack.health_of("engine") == "healthy", timeout=300)
    timed(report, healthy, "the engine is healthy again after a restart", started)
    report.add(
        stack.health_of("worker") == "running",
        f"the worker survived the engine restart ({worker_verdict(stack, since)})",
    )
    ok, detail = doctor_green(stack.clone)
    report.add(ok, f"doctor green after an engine restart ({detail})")


def step_database_outage(report: Report, stack: Stack) -> None:
    """One login attempt, not a poll: core allows five a minute and the database is gone the
    instant the container stops. The cms container keeps reporting healthy throughout, because
    its healthcheck fetches the admin login page, which renders without touching Postgres."""
    started = time.monotonic()
    neops(stack.clone, "compose", "--", "stop", "postgres-cms")
    outage = stack.admin_login()
    timed(
        report,
        outage.token is None,
        f"the CMS refuses a login while its database is stopped ({outage.error[:120]}), "
        f"though the container still reports {stack.health_of('cms')}",
        started,
    )
    neops(stack.clone, "compose", "--", "start", "postgres-cms")
    back = wait_for(lambda: stack.health_of("postgres-cms") == "healthy", timeout=300)
    timed(report, back, "the CMS database is healthy again", started)
    admin_can_log_in(report, stack, "once the database returns")
    ok, detail = doctor_green(stack.clone)
    report.add(ok, f"doctor green after the database outage ({detail})")


def steps_by_name(report: Report, stack: Stack) -> dict[str, Callable[[], None]]:
    """`prune` is `restart` with a host-wide prune in the middle; the assertions are the same."""
    return {
        "kill": lambda: step_kill_and_recover(report, stack),
        "restart": lambda: step_restart_survival(report, stack, prune=False),
        "prune": lambda: step_restart_survival(report, stack, prune=True),
        "env": lambda: step_broken_env(report, stack),
        "engine": lambda: step_engine_restart(report, stack),
        "database": lambda: step_database_outage(report, stack),
    }


def selected_steps(only: list[str] | None, prune: bool) -> list[str]:
    if only:
        return only
    return [("prune" if name == "restart" and prune else name) for name in DEFAULT_STEPS]


def run_steps(report: Report, stack: Stack, names: list[str]) -> None:
    steps = steps_by_name(report, stack)
    for name in names:
        print(f"\n=== {name} ===", flush=True)
        try:
            steps[name]()
        except Exception as exc:  # one broken step must not hide the others
            report.add(False, f"step {name!r} raised {type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clone", type=Path, help="the clone directory run_scenario.py --keep left behind")
    ap.add_argument("--prune", action="store_true", help=PRUNE_WARNING)
    ap.add_argument(
        "--only",
        action="append",
        choices=STEP_NAMES,
        metavar="STEP",
        help=f"run only this step, repeatable and ordered: {', '.join(STEP_NAMES)}",
    )
    args = ap.parse_args(argv)

    clone = args.clone.resolve()
    if not (clone / ".env").is_file():
        raise SystemExit(f"{clone} does not look like an installed deployment: no .env")

    stack = load_stack(clone)
    report = Report()
    started = time.monotonic()
    run_steps(report, stack, selected_steps(args.only, args.prune))
    print(
        f"\nchaos: {len(report.results) - len(report.failed)}/{len(report.results)} assertions "
        f"passed in {time.monotonic() - started:.0f}s",
        flush=True,
    )
    for message in report.failed:
        print(f"  FAILED: {message}", flush=True)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
