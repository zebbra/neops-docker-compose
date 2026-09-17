from __future__ import annotations

import concurrent.futures
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from neops_compose.compose import DOCKER_MISSING, Compose, ComposeError
from neops_compose.databases import DATABASES
from neops_compose.env import Env, MissingEnv
from neops_compose.ports import (
    DEFAULT_GRAFANA_PORT,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_KEYCLOAK_PORT,
    DEFAULT_MONITOR_PORT,
)
from neops_compose.rules import problems
from neops_compose.scenario import Scenario
from neops_compose.urls import BadUrl, monitor_entrypoint_wanted, public_urls

MIN_COMPOSE = (2, 24)
MIN_MAX_MAP_COUNT = 262144
REGISTRY_WORKERS = 6

# What Elasticsearch demands before it allocates a shard. Mirrored from compose.yaml, which the
# CLI cannot read at runtime (it ships no YAML parser); test_compose_files holds the two together.
ES_HIGH_WATERMARK = 0.90
ES_DEFAULT_HEADROOM = 150 * 2**30  # Elasticsearch's own default for high.max_headroom

_SIZE_UNITS = {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40, "P": 2**50}


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


def _monitor_port_published(env: Env, scenario: Scenario) -> bool:
    """URLs the .env check has already rejected still get their ports reserved: a port
    conflict is worth reporting alongside, and a spare reservation costs nothing."""
    try:
        return monitor_entrypoint_wanted(public_urls(env, scenario), scenario)
    except (MissingEnv, BadUrl):
        return scenario.shared_host


def _proxy_ports(env: Env, scenario: Scenario) -> list[tuple[str, int]]:
    if scenario.proxy == "traefik":
        ports = [("0.0.0.0", int(env.get("NEOPS_HTTP_PORT", str(DEFAULT_HTTP_PORT))))]
        if scenario.tls:
            ports.append(("0.0.0.0", int(env.get("NEOPS_HTTPS_PORT", str(DEFAULT_HTTPS_PORT)))))
        if _monitor_port_published(env, scenario):
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


def required_ports(env: Env, scenario: Scenario) -> list[tuple[str, int]]:
    """Keycloak and Grafana publish on NEOPS_BIND_ADDRESS in both proxy modes: their overlays
    map the port unconditionally, so Traefik in front of them reserves nothing."""
    ports = _proxy_ports(env, scenario)
    bind = env.get("NEOPS_BIND_ADDRESS", "127.0.0.1")
    if scenario.keycloak:
        ports.append((bind, int(env.get("NEOPS_KEYCLOAK_PORT", str(DEFAULT_KEYCLOAK_PORT)))))
    if scenario.metrics:
        ports.append((bind, int(env.get("NEOPS_GRAFANA_PORT", str(DEFAULT_GRAFANA_PORT)))))
    return ports


def _cmd(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(args, text=True, capture_output=True)
    except FileNotFoundError:
        return 127, DOCKER_MISSING
    return r.returncode, (r.stdout or r.stderr).strip()


def _detail(rc: int, out: str, failure: str) -> str:
    """A missing binary explains itself; any other failure gets the actionable sentence."""
    if rc == 0 or out == DOCKER_MISSING:
        return out
    return failure


def _docker_checks() -> tuple[list[Check], bool, bool]:
    daemon_rc, docker_v = _cmd("docker", "version", "--format", "{{.Server.Version}}")
    compose_rc, compose_v = _cmd("docker", "compose", "version", "--short")
    checks = [
        Check(
            "docker daemon",
            daemon_rc == 0,
            _detail(daemon_rc, docker_v, "docker is not running or not reachable"),
        ),
        Check(
            "docker compose",
            compose_rc == 0 and version_ok(compose_v, MIN_COMPOSE),
            _detail(compose_rc, compose_v, "docker compose v2 is required"),
        ),
    ]
    return checks, daemon_rc == 0, compose_rc == 0


def _env_checks(env: Env, scenario: Scenario, repo: Path) -> list[Check]:
    found = [Check(".env", False, p) for p in problems(env, scenario, repo)]
    if found:
        return found
    return [Check(".env", True, f"{len(scenario.files)} compose files, scenario valid")]


def parse_es_size(raw: str) -> int | None:
    """An Elasticsearch byte size ("5GB", "512m"); None when the text is not one."""
    text = raw.strip().upper().removesuffix("B")
    digits = text.rstrip("KMGTP")
    unit = text[len(digits) :]
    if not digits.isdigit() or unit not in _SIZE_UNITS:
        return None
    return int(digits) * _SIZE_UNITS[unit]


def es_free_space_needed(env: Env, total: int) -> int | None:
    """The free space Elasticsearch demands before it allocates a shard, or None when
    NEOPS_ES_HEADROOM is set to something that is not a byte size."""
    cap = ES_DEFAULT_HEADROOM
    if env.is_set("NEOPS_ES_HEADROOM"):
        cap = parse_es_size(env.get("NEOPS_ES_HEADROOM"))
        if cap is None:
            return None
    return min(round((1 - ES_HIGH_WATERMARK) * total), cap)


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


def disk_check(env: Env, total: int, free: int) -> Check:
    """Elasticsearch refuses every shard while the filesystem sits above its high watermark, and
    the blocked shard surfaces only as cms-init's index creation timing out thirty seconds later."""
    needed = es_free_space_needed(env, total)
    if needed is None:
        raw = env.get("NEOPS_ES_HEADROOM")
        return Check("disk", False, f"NEOPS_ES_HEADROOM={raw!r} is not a byte size (try 5GB)")
    detail = f"{_gib(free)} free, Elasticsearch needs {_gib(needed)}"
    if free >= needed:
        return Check("disk", True, detail)
    demand = (
        f"{1 - ES_HIGH_WATERMARK:.0%} of the {_gib(total)} filesystem "
        f"at watermark.high={ES_HIGH_WATERMARK:.0%}"
    )
    return Check(
        "disk",
        False,
        f"{detail} ({demand}, capped by NEOPS_ES_HEADROOM); free space or lower NEOPS_ES_HEADROOM",
    )


def _host_checks(env: Env, repo: Path, data: Path) -> list[Check]:
    usage = shutil.disk_usage(data if data.exists() else repo)
    out = [disk_check(env, usage.total, usage.free)]
    mmc = Path("/proc/sys/vm/max_map_count")
    if not mmc.exists():
        return out
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
    return out


def _port_checks(env: Env, scenario: Scenario, running: set[str]) -> list[Check]:
    if running:
        return [Check("ports", True, "skipped: the stack is running")]
    out = []
    for address, port in required_ports(env, scenario):
        free = port_free(address, port)
        out.append(Check("ports", free, f"{address}:{port}" + ("" if free else " is in use")))
    return out


def _image_check(image: str) -> Check:
    """A cached image needs no registry round trip, and works offline."""
    if _cmd("docker", "image", "inspect", image)[0] == 0:
        return Check("image", True, f"{image} (local)")
    rc, detail = _cmd("docker", "manifest", "inspect", image)
    if rc == 0:
        return Check("image", True, image)
    reason = detail.splitlines()[-1] if detail else "unknown"
    return Check("image", False, f"{image}: not pullable ({reason}); run docker login quay.io")


def image_checks(compose: Compose) -> list[Check]:
    try:
        images = compose.images()
    except ComposeError as exc:
        first_line = str(exc).splitlines()[0]
        return [
            Check(
                "image",
                False,
                f"docker compose config failed: {first_line}; run ./neops render first, "
                "or ./neops check --no-images before the first install",
            )
        ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=REGISTRY_WORKERS) as pool:
        return list(pool.map(_image_check, images))


def _db_password_checks(env: Env, compose: Compose, running: set[str]) -> list[Check]:
    out = []
    for database in DATABASES:
        if database.service not in running or not env.is_set(database.env_key):
            continue
        try:
            compose.exec(
                database.service,
                "psql",
                "-U",
                database.role,
                "-d",
                database.name,
                "-c",
                "select 1",
                env={"PGPASSWORD": env.get(database.env_key)},
            )
            out.append(Check("db password", True, f"{database.service} accepts {database.env_key}"))
        except Exception:
            out.append(
                Check(
                    "db password",
                    False,
                    f"{database.service} rejects {database.env_key}: the value in .env changed "
                    "without ./neops rotate db-password; restore it or rotate properly",
                )
            )
    return out


def run_checks(
    env: Env, scenario: Scenario, repo: Path, data: Path, compose: Compose, check_images: bool = True
) -> list[Check]:
    out, daemon_ok, _ = _docker_checks()
    if not env.exists:
        out.append(Check(".env", False, "no .env file: copy one of examples/*.env to .env"))
        return out
    env_checks = _env_checks(env, scenario, repo)
    out += env_checks
    out += _host_checks(env, repo, data)

    running: set[str] = set()
    if daemon_ok:
        try:
            running = compose.running_services()
        except Exception:
            running = set()
    out += _port_checks(env, scenario, running)
    if check_images and daemon_ok and all(c.ok for c in env_checks):
        out += image_checks(compose)
    if daemon_ok:
        out += _db_password_checks(env, compose, running)
    return out


def all_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)


def format_report(checks: list[Check]) -> str:
    return "\n".join(f"{'OK  ' if c.ok else 'FAIL'} {c.name:<16} {c.detail}".rstrip() for c in checks)
