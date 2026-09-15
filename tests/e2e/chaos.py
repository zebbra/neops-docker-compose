#!/usr/bin/env python3
"""Chaos checks against a stack left running by run_scenario.py --keep.

    uv run python tests/e2e/chaos.py <clone dir> [--prune]

1. Kill each service in turn; `compose up -d --wait` brings it back and doctor is green again.
2. Stop and start the stack; the admin account, the engine's API key and the state survive.
3. Corrupt .env three ways; `./neops check` names each problem and exits 1.
4. Restart the engine under a polling worker; the worker keeps working.
5. Stop the CMS database; logins fail while it is gone and work again when it returns.

`--prune` adds `docker system prune -a --volumes` between the stop and the start of step 2.
It removes every unused image on the host, so it is off by default and asks before running.
Exit code 0 only when every step passed. See tests/e2e/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_scenario import CONNECT, Report, neops  # noqa: E402

from neops_compose.compose import Compose  # noqa: E402
from neops_compose.doctor import NAME_WIDTH, Http, Login, login  # noqa: E402
from neops_compose.urls import PublicUrl  # noqa: E402

KILL_SERVICES = ("cms", "engine", "redis", "postgres-cms", "worker", "traefik")
WAIT_TIMEOUT = "600"
POLLING_MARKER = "Worker ready and listening for jobs"
# The plan asked for a device written through `deviceUpsert` here. core's GraphQL writes are
# role-gated and `install` seeds a superuser holding no NeOps role, so a shipped deployment
# answers "User is not allowed to create a group." until an operator grants one from the CMS
# admin site (docs/10-install.md, "Adding users"). Persistence is asserted through what the
# deployment does provide: the admin account, the minted API key and the CLI's own state.

# Core allows five logins a minute from one address and doctor spends two of them, so a
# doctor run right after another can fail on the rate limit alone. That is not a recovery
# failure: wait out the window and ask again.
RATE_LIMIT_MARKERS = ("rate limit", "too many")
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


def prune_host(report: Report) -> None:
    if not confirm_prune():
        report.add(False, "prune was requested but not confirmed")
        return
    subprocess.run(["docker", "system", "prune", "-a", "--volumes", "-f"], check=True)


def step_restart_survival(report: Report, stack: Stack, prune: bool) -> None:
    started = time.monotonic()
    admin_can_log_in(report, stack, "before the restart")
    engine_env_before = stack.engine_env.read_bytes()
    keys_before = stack.api_keys()
    neops(stack.clone, "down")
    if prune:
        prune_host(report)
    neops(stack.clone, "up", "--connect", CONNECT, "--insecure")
    what = "after a prune and a restart" if prune else "after a stop and a start"
    admin_can_log_in(report, stack, what)
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


def run_steps(report: Report, stack: Stack, prune: bool) -> None:
    steps = (
        ("kill and recover", lambda: step_kill_and_recover(report, stack)),
        ("restart survival", lambda: step_restart_survival(report, stack, prune)),
        ("broken .env", lambda: step_broken_env(report, stack)),
        ("engine restart", lambda: step_engine_restart(report, stack)),
        ("database outage", lambda: step_database_outage(report, stack)),
    )
    for name, step in steps:
        print(f"\n=== {name} ===", flush=True)
        try:
            step()
        except Exception as exc:  # one broken step must not hide the others
            report.add(False, f"step {name!r} raised {type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clone", type=Path, help="the clone directory run_scenario.py --keep left behind")
    ap.add_argument("--prune", action="store_true", help=PRUNE_WARNING)
    args = ap.parse_args(argv)

    clone = args.clone.resolve()
    if not (clone / ".env").is_file():
        raise SystemExit(f"{clone} does not look like an installed deployment: no .env")

    stack = load_stack(clone)
    report = Report()
    started = time.monotonic()
    run_steps(report, stack, args.prune)
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
