#!/usr/bin/env python3
"""End-to-end run of one examples/*.env scenario in a throwaway clone.

    uv run python tests/e2e/run_scenario.py traefik-tls-selfsigned [--port-base 18000] [--keep]

Clones this checkout, writes a .env derived from the example with .localhost hostnames,
ports derived from --port-base and generated secrets, installs the stack and asserts the
deployment works. Exit code 0 only when every assertion held. See tests/e2e/README.md.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from neops_compose.compose import Compose  # noqa: E402
from neops_compose.doctor import NAME_WIDTH, Http, login  # noqa: E402
from neops_compose.scenario import OVERRIDE_FILE, Scenario  # noqa: E402
from neops_compose.urls import PublicUrl  # noqa: E402

EXAMPLE_DOMAIN = "neops.example.com"
TEST_DOMAIN = "neops.localhost"
CONNECT = "127.0.0.1"
DENY_PROBE = "engine worker API denied"
URL_KEYS = (
    "NEOPS_WEB_URL",
    "NEOPS_CMS_URL",
    "NEOPS_ENGINE_URL",
    "NEOPS_WORKFLOWS_URL",
    "NEOPS_KEYCLOAK_URL",
    "NEOPS_GRAFANA_URL",
)
SECRET_KEYS = (
    "NEOPS_CMS_DB_PASSWORD",
    "NEOPS_ENGINE_DB_PASSWORD",
    "DJANGO_SECRET_KEY",
    "NEOPS_ADMIN_PASSWORD",
    "NEOPS_KEYCLOAK_ADMIN_PASSWORD",
    "NEOPS_KEYCLOAK_DB_PASSWORD",
    "NEOPS_GRAFANA_ADMIN_PASSWORD",
)
OVERRIDE_YAML = """# Written by tests/e2e/run_scenario.py: this dev box runs above Elasticsearch's
# 95% flood stage, which would put every index into read-only mode.
services:
  elasticsearch:
    environment:
      - cluster.routing.allocation.disk.threshold_enabled=false
"""


@dataclass(frozen=True)
class Ports:
    """Every published port of one run, derived from a base so runs can coexist."""

    base: int

    @property
    def http(self) -> int:
        return self.base + 80

    @property
    def https(self) -> int:
        return self.base + 443

    @property
    def monitor(self) -> int:
        return self.base + 444

    @property
    def keycloak(self) -> int:
        return self.base + 180

    @property
    def grafana(self) -> int:
        return self.base + 300

    @property
    def expose_cms(self) -> int:
        return self.base

    @property
    def expose_engine(self) -> int:
        return self.base + 30

    @property
    def expose_monitor(self) -> int:
        return self.base + 31


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[bool, str]] = []

    def add(self, ok: bool, message: str) -> bool:
        self.results.append((bool(ok), message))
        print(("PASS " if ok else "FAIL ") + message, flush=True)
        return bool(ok)

    @property
    def failed(self) -> list[str]:
        return [m for ok, m in self.results if not ok]


def sh(cmd: list[str], cwd: Path, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=capture)


def neops(clone: Path, *args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return sh(["./neops", *args], cwd=clone, check=check, capture=capture)


def read_example(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().replace(EXAMPLE_DOMAIN, TEST_DOMAIN)
    return values


def fill_secrets(values: dict[str, str]) -> None:
    for key in SECRET_KEYS:
        if key in values and not values[key]:
            values[key] = secrets.token_hex(24)


def _at(url: str, scheme: str, port: int) -> str:
    parsed = PublicUrl.parse(url)
    return f"{scheme}://{parsed.host}:{port}{parsed.path}"


def apply_expose_ports(values: dict[str, str], ports: Ports) -> None:
    values["NEOPS_BIND_ADDRESS"] = CONNECT
    values["NEOPS_WEB_PORT"] = str(ports.http)
    values["NEOPS_CMS_PORT"] = str(ports.expose_cms)
    values["NEOPS_ENGINE_PORT"] = str(ports.expose_engine)
    values["NEOPS_MONITOR_PORT"] = str(ports.expose_monitor)
    values["NEOPS_WEB_URL"] = f"http://{TEST_DOMAIN}:{ports.http}"
    values["NEOPS_CMS_URL"] = f"http://cms.{TEST_DOMAIN}:{ports.expose_cms}"
    values["NEOPS_ENGINE_URL"] = f"http://engine.{TEST_DOMAIN}:{ports.expose_engine}"
    values["NEOPS_WORKFLOWS_URL"] = f"http://workflows.{TEST_DOMAIN}:{ports.expose_monitor}"


def apply_traefik_ports(values: dict[str, str], scenario: Scenario, ports: Ports) -> None:
    scheme = "https" if scenario.tls else "http"
    public = ports.https if scenario.tls else ports.http
    values["NEOPS_HTTP_PORT"] = str(ports.http)
    if scenario.tls:
        values["NEOPS_HTTPS_PORT"] = str(ports.https)
    for key in URL_KEYS:
        if key in values:
            values[key] = _at(values[key], scheme, public)
    if scenario.shared_host:
        values["NEOPS_MONITOR_PORT"] = str(ports.monitor)
        values["NEOPS_WORKFLOWS_URL"] = _at(values["NEOPS_WORKFLOWS_URL"], scheme, ports.monitor)
    if scenario.keycloak:
        values["NEOPS_KEYCLOAK_PORT"] = str(ports.keycloak)
    if scenario.metrics:
        values["NEOPS_GRAFANA_PORT"] = str(ports.grafana)


def use_self_signed_tls(values: dict[str, str], scenario: Scenario) -> None:
    """No run needs an operator-provided certificate: every tls-files example is switched over."""
    if scenario.tls != "files":
        return
    values["NEOPS_TLS_SELF_SIGNED"] = "true"
    values["NEOPS_TLS_CERT_FILE"] = "./data/secrets/tls/cert.pem"
    values["NEOPS_TLS_KEY_FILE"] = "./data/secrets/tls/key.pem"


def scenario_of(values: dict[str, str]) -> Scenario:
    return Scenario(tuple(f.strip() for f in values["COMPOSE_FILE"].split(":") if f.strip()))


def build_env(example: Path, name: str, ports: Ports) -> dict[str, str]:
    values = read_example(example)
    scenario = scenario_of(values)
    fill_secrets(values)
    use_self_signed_tls(values, scenario)
    if scenario.proxy == "expose":
        apply_expose_ports(values, ports)
    else:
        apply_traefik_ports(values, scenario, ports)
    values["COMPOSE_FILE"] = values["COMPOSE_FILE"] + ":" + OVERRIDE_FILE
    values["COMPOSE_PROJECT_NAME"] = f"neops-e2e-{name}-{ports.base}"
    values["NEOPS_MONITOR_IMAGE"] = "neops-monitor-app:local"
    return values


def prepare_clone(work: Path, values: dict[str, str]) -> Path:
    clone = work / "repo"
    work.mkdir(parents=True, exist_ok=True)
    if not clone.exists():
        sh(["git", "clone", "-q", str(REPO), str(clone)], cwd=work)
        sh(["git", "checkout", "-q", "release/2.0"], cwd=clone)
    (clone / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    (clone / OVERRIDE_FILE).write_text(OVERRIDE_YAML)
    return clone


def failing_probe_names(clone: Path) -> list[str]:
    """doctor prints `FAIL <name padded to NAME_WIDTH> <detail>`."""
    out = neops(clone, "doctor", "--connect", CONNECT, "--insecure", check=False, capture=True)
    print(out.stdout + out.stderr, flush=True)
    start = len("FAIL ")
    return [
        line[start : start + NAME_WIDTH].strip()
        for line in out.stdout.splitlines()
        if line.startswith("FAIL")
    ]


def install(report: Report, clone: Path, expose: bool, label: str) -> bool:
    """In expose mode there is no proxy to deny the worker routes, so doctor fails by design."""
    code = neops(clone, "install", "--connect", CONNECT, "--insecure", check=False).returncode
    if not expose:
        return report.add(code == 0, f"{label} succeeded")
    if code == 0:
        return report.add(False, f"{label}: doctor passed but the worker API deny probe should fail here")
    names = failing_probe_names(clone)
    return report.add(
        names == [DENY_PROBE],
        f"{label}: the worker API deny probe is the only failure (got {names})",
    )


def assert_admin_login(report: Report, values: dict[str, str], http: Http) -> None:
    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    result = login(cms, http, values.get("NEOPS_ADMIN_USER", "neops"), values["NEOPS_ADMIN_PASSWORD"])
    report.add(bool(result.token), f"admin login returns an access token ({result.error})")


def assert_oidc_seeded(report: Report, values: dict[str, str], http: Http) -> None:
    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    query = '{"query":"{appSettings{oidcProviders{id name loginUrl}}}"}'
    status, text = http.request(cms, "/graphql", method="POST", body=query)
    ok = status == 200 and "oidcProviders" in text and "keycloak" in text.lower()
    report.add(ok, f"OIDC providers are seeded ({status}: {text[:160]})")


def api_key_count(clone: Path) -> int:
    state = json.loads((clone / "data" / ".neops" / "state.json").read_text())
    return len(state["api_keys"])


def assert_idempotent(report: Report, clone: Path, expose: bool) -> None:
    before = api_key_count(clone)
    install(report, clone, expose, "second install")
    after = api_key_count(clone)
    report.add(before == after == 1, f"one API key before and after the second install ({before} -> {after})")


def assert_backup(report: Report, clone: Path) -> None:
    neops(clone, "backup")
    archives = sorted(p for p in (clone / "backups").iterdir() if p.is_dir())
    if not report.add(len(archives) == 1, f"backup created one archive ({len(archives)})"):
        return
    cms_dump = archives[0] / "cms.dump"
    report.add(cms_dump.exists() and cms_dump.stat().st_size > 1000, "backup holds a non-trivial cms.dump")
    report.add((archives[0] / "engine.dump").exists(), "backup holds engine.dump")


def assert_worker(report: Report, clone: Path) -> None:
    worker = next((r for r in Compose(clone).ps() if r.get("Service") == "worker"), None)
    if not report.add(worker is not None, "the worker container exists"):
        return
    report.add(worker.get("State") == "running", f"the worker container is running ({worker.get('State')})")
    logs = neops(clone, "compose", "--", "logs", "--no-log-prefix", "worker", capture=True).stdout.lower()
    report.add(
        "function block" in logs or "registered" in logs, "the worker log shows function blocks registering"
    )


def assert_shared_host_routing(report: Report, values: dict[str, str], http: Http) -> None:
    web = PublicUrl.parse(values["NEOPS_WEB_URL"])
    engine = PublicUrl.parse(values["NEOPS_ENGINE_URL"])
    status, text = http.request(web, "/admin/login/")
    report.add(status == 200, f"core admin answers on the web hostname ({status})")
    report.add("/djstatic/" in text, "the admin page references /djstatic/")
    status, _ = http.request(web, "/djstatic/admin/css/base.css")
    report.add(status == 200, f"core static files are served under /djstatic/ ({status})")
    status, _ = http.request(engine, "/health")
    report.add(status == 200, f"the engine answers under {engine.path}/health ({status})")


def run_assertions(report: Report, clone: Path, values: dict[str, str], scenario: Scenario) -> None:
    http = Http(connect=CONNECT, insecure=True)
    if scenario.oidc:
        assert_oidc_seeded(report, values, http)
    else:
        assert_admin_login(report, values, http)
    if scenario.shared_host:
        assert_shared_host_routing(report, values, http)
    assert_worker(report, clone)
    assert_backup(report, clone)
    assert_idempotent(report, clone, scenario.proxy == "expose")


def remove_work(work: Path) -> None:
    """Postgres and Elasticsearch write as other uids; a container does the deleting."""
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{work}:/work",
            "postgres:16-alpine",
            "rm",
            "-rf",
            "/work/repo/data",
        ],
        check=False,
    )
    shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario")
    ap.add_argument("--port-base", type=int, default=18000)
    ap.add_argument("--keep", action="store_true", help="leave the stack running and the clone in place")
    ap.add_argument("--workdir", type=Path)
    args = ap.parse_args(argv)

    example = REPO / "examples" / f"{args.scenario}.env"
    if not example.exists():
        raise SystemExit(f"no such example: {example}")
    if args.scenario == "traefik-acme":
        raise SystemExit(
            "traefik-acme needs public DNS and port 80; it is covered by the compose-config gate"
        )

    ports = Ports(args.port_base)
    values = build_env(example, args.scenario, ports)
    scenario = scenario_of(values)
    work = args.workdir or Path(f"/tmp/neops-e2e/{args.scenario}-{ports.base}")
    clone = prepare_clone(work, values)
    print(f"workdir {work}", flush=True)

    report = Report()
    started = time.monotonic()
    try:
        neops(clone, "check", "--no-images")
        if install(report, clone, scenario.proxy == "expose", "install"):
            run_assertions(report, clone, values, scenario)
    finally:
        elapsed = time.monotonic() - started
        if args.keep:
            print(f"stack left running in {clone}", flush=True)
        else:
            neops(clone, "down", check=False)
            remove_work(work)
    print(
        f"\n{args.scenario}: {len(report.results) - len(report.failed)}/{len(report.results)} assertions "
        f"passed in {elapsed:.0f}s",
        flush=True,
    )
    for message in report.failed:
        print(f"  FAILED: {message}", flush=True)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
