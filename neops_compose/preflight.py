from __future__ import annotations

import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from neops_compose.compose import Compose
from neops_compose.env import Env
from neops_compose.ports import DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT, DEFAULT_MONITOR_PORT
from neops_compose.rules import problems
from neops_compose.scenario import Scenario

MIN_COMPOSE = (2, 24)
MIN_DISK_GIB = 5
MIN_MAX_MAP_COUNT = 262144


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def version_ok(raw: str, minimum: tuple[int, int]) -> bool:
    nums = [int(x) for x in raw.lstrip("v").split("-")[0].split(".") if x.isdigit()]
    return tuple(nums[:2]) >= minimum


def port_free(address: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((address, port))
            return True
        except OSError:
            return False


def required_ports(env: Env, scenario: Scenario) -> list[tuple[str, int]]:
    if scenario.proxy == "traefik":
        ports = [("0.0.0.0", int(env.get("NEOPS_HTTP_PORT", str(DEFAULT_HTTP_PORT))))]
        if scenario.tls:
            ports.append(("0.0.0.0", int(env.get("NEOPS_HTTPS_PORT", str(DEFAULT_HTTPS_PORT)))))
        if scenario.shared_host:
            ports.append(("0.0.0.0", int(env.get("NEOPS_MONITOR_PORT", str(DEFAULT_MONITOR_PORT)))))
        return ports
    bind = env.get("NEOPS_BIND_ADDRESS", "127.0.0.1")
    return [
        (bind, int(env.get(k, d)))
        for k, d in (
            ("NEOPS_WEB_PORT", "8080"),
            ("NEOPS_CMS_PORT", "8000"),
            ("NEOPS_ENGINE_PORT", "3030"),
            ("NEOPS_MONITOR_PORT", "3031"),
        )
    ]


def _cmd(*args: str) -> tuple[int, str]:
    r = subprocess.run(args, text=True, capture_output=True)
    return r.returncode, (r.stdout or r.stderr).strip()


def run_checks(
    env: Env, scenario: Scenario, repo: Path, data: Path, compose: Compose, check_images: bool = True
) -> list[Check]:
    out: list[Check] = []
    code, docker_v = _cmd("docker", "version", "--format", "{{.Server.Version}}")
    out.append(
        Check("docker daemon", code == 0, docker_v if code == 0 else "docker is not running or not reachable")
    )
    code, compose_v = _cmd("docker", "compose", "version", "--short")
    out.append(
        Check(
            "docker compose",
            code == 0 and version_ok(compose_v, MIN_COMPOSE),
            compose_v if code == 0 else "docker compose v2 is required",
        )
    )
    if not env.exists:
        out.append(Check(".env", False, "no .env file: copy one of examples/*.env to .env"))
        return out
    for p in problems(env, scenario, repo):
        out.append(Check(".env", False, p))
    if not any(c.name == ".env" for c in out):
        out.append(Check(".env", True, f"{len(scenario.files)} compose files, scenario valid"))

    free_gib = shutil.disk_usage(data if data.exists() else repo).free / 2**30
    out.append(Check("disk", free_gib >= MIN_DISK_GIB, f"{free_gib:.1f} GiB free under {data}"))

    mmc = Path("/proc/sys/vm/max_map_count")
    if mmc.exists():
        value = int(mmc.read_text().strip())
        enough = value >= MIN_MAX_MAP_COUNT
        detail = (
            f"{value}"
            if enough
            else f"{value} < {MIN_MAX_MAP_COUNT}; run: "
            f"sudo sysctl -w vm.max_map_count={MIN_MAX_MAP_COUNT} "
            "and persist it in /etc/sysctl.d/99-neops.conf"
        )
        out.append(Check("vm.max_map_count", enough, detail))

    try:
        running = compose.running_services() if code == 0 else set()
    except Exception:
        running = set()
    if not running:
        for address, port in required_ports(env, scenario):
            free = port_free(address, port)
            out.append(Check("ports", free, f"{address}:{port}" + ("" if free else " is in use")))
    if check_images and code == 0 and not any(c.name == ".env" and not c.ok for c in out):
        for image in compose.images():
            rc, detail = _cmd("docker", "manifest", "inspect", image)
            out.append(
                Check(
                    "image",
                    rc == 0,
                    image
                    if rc == 0
                    else f"{image}: not pullable ({detail.splitlines()[-1] if detail else 'unknown'}); "
                    "run docker login quay.io",
                )
            )
    for service, user, db, key in (
        ("postgres-cms", "neops", "neops", "NEOPS_CMS_DB_PASSWORD"),
        ("postgres-engine", "postgres", "neops-workflow", "NEOPS_ENGINE_DB_PASSWORD"),
        ("postgres-keycloak", "keycloak", "keycloak", "NEOPS_KEYCLOAK_DB_PASSWORD"),
    ):
        if service in running and env.is_set(key):
            try:
                compose.exec(
                    service, "psql", "-U", user, "-d", db, "-c", "select 1", env={"PGPASSWORD": env.get(key)}
                )
                out.append(Check("db password", True, f"{service} accepts {key}"))
            except Exception:
                out.append(
                    Check(
                        "db password",
                        False,
                        f"{service} rejects {key}: the value in .env changed without "
                        "./neops rotate db-password; restore it or rotate properly",
                    )
                )
    return out


def all_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)


def format_report(checks: list[Check]) -> str:
    return "\n".join(f"{'OK  ' if c.ok else 'FAIL'} {c.name:<16} {c.detail}".rstrip() for c in checks)
