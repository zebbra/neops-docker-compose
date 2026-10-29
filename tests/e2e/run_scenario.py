#!/usr/bin/env python3
"""End-to-end run of one examples/*.env scenario in a throwaway clone.

    uv run python tests/e2e/run_scenario.py traefik-tls-selfsigned [--port-base 18000] [--keep]

Clones this checkout, writes a .env derived from the example with .localhost hostnames,
ports derived from --port-base and generated secrets, installs the stack and asserts the
deployment works. Exit code 0 only when every assertion held. See tests/e2e/README.md.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import inside  # noqa: E402
import logrecords  # noqa: E402
import upgrade_hook  # noqa: E402
import workflows  # noqa: E402
from guards import SHELL_COMPOSE_KEYS, shell_compose_refusal  # noqa: E402,F401

from cms import GROUP_DELETE, GROUP_READ, GROUP_UPSERT, gql  # noqa: E402
from neops_compose.backup import ARCHIVE_NAME  # noqa: E402
from neops_compose.compose import Compose  # noqa: E402
from neops_compose.doctor import DENY_LOG_PROBE, DENY_PROBE, NAME_WIDTH, Http, login  # noqa: E402
from neops_compose.env import Env  # noqa: E402
from neops_compose.rules import LOG_BACKEND  # noqa: E402
from neops_compose.scenario import OVERRIDE_FILE, Scenario  # noqa: E402
from neops_compose.state import State  # noqa: E402
from neops_compose.urls import MonitorPlacement, PublicUrl, monitor_placement  # noqa: E402

EXAMPLE_DOMAIN = "neops.example.com"
TEST_DOMAIN = "neops.localhost"
CONNECT = "127.0.0.1"
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
METRICS_SERVICES = (
    "celery-exporter",
    "redis-exporter",
    "postgres-exporter-cms",
    "postgres-exporter-engine",
    "elasticsearch-exporter",
    "victoriametrics",
    "vmalert",
    "grafana",
)
SCRAPE_JOBS = frozenset({"celery", "elasticsearch", "postgres", "redis", "victoriametrics", "vmalert"})
GROUP_NAME = "e2e-admin-writes"
SURVIVOR_GROUP = "e2e-upgrade-survivor"
CMS_TASK_SERVICES = ("cms-worker", "cms-beat")
LEGACY_DEVICE = "e2e-legacy-task"
LEGACY_DEVICE_IP = "192.0.2.10"
LEGACY_TASK = "e2e-static-facts"
LEGACY_FACTS_KEY = "e2e"
STATIC_FACTS_PROVIDER = "providers.core.neops.io/generic-static-facts:1"
STATIC_FACTS_KWARGS = {
    "facts_key": LEGACY_FACTS_KEY,
    "run_on": "DEVICE",
    "merge_overwrite": "OVERWRITE",
    "mapping_style": "JSON",
    "mapping_template": '{"source": "e2e", "hostname": "{{ device.hostname }}"}',
}
DEVICE_UPSERT = (
    "mutation($h:String!,$ip:String!,$p:ID){deviceUpsert(hostname:$h,ip:$ip,platform:$p){device{id}}}"
)
DEVICE_FACTS = "query($id:Decimal){devices(id:$id){results{facts}}}"
DEVICE_DELETE = "mutation($id:ID!){deviceDelete(id:$id,hardDelete:true){device{id}}}"
TASK_UPSERT = (
    "mutation($n:String!,$p:String!,$k:String!){neopsTaskUpsert("
    "name:$n,uniquetaskname:$n,providerIdentifier:$p,taskKwargs:$k){neopsTask{id}}}"
)
TASK_EXECUTE = (
    "mutation($id:ID!,$on:[ID]){executeNeopsTask("
    "neopsTaskId:$id,executeOn:$on,executeOnType:DEVICE,dryRun:false){executions{id}}}"
)
TASK_DELETE = "mutation($id:ID!){neopsTaskDelete(id:$id,hardDelete:true){neopsTask{id}}}"
EXECUTION_STATE = "query($id:Decimal){executions(id:$id){results{state taskLog}}}"
EXECUTION_SETTLED = frozenset({"SUCCESSFUL", "FAILED", "PARTIAL_FAILED", "ABORTED"})
EXECUTION_TIMEOUT = 180.0
LEGACY_PLATFORM = "Linux Generic"
PLATFORM_READ = "query($n:String!){platforms(name:$n){results{id}}}"
RATE_LIMIT_WINDOW = 65.0
VM_TARGETS_URL = "http://127.0.0.1:8428/api/v1/targets"
GRAFANA_DATASOURCES = "http://127.0.0.1:3000/api/datasources"
GRAFANA_DASHBOARDS = "http://127.0.0.1:3000/api/search?type=dash-db"
# An install that enabled the CMS's retention and never set its days: the case migration 0003
# must carry over as 90d, the old default, rather than let it fall to the new 30d.
UPGRADE_ENV = {"EXECUTION_LOG_RETENTION_ENABLED": "True"}
HARNESS_OVERLAYS = "E2E_OVERLAYS"
E2E_EXAMPLES = REPO / "tests" / "e2e" / "examples"
GRAFANA_AND_LOGS_MIGRATION = "0003_grafana_and_logs_layout"
NOT_CONFIGURED = "LOGS_NOT_CONFIGURED"
E2E_PROJECT_PREFIX = "neops-e2e-"
OVERRIDE_MARKER = "# Written by tests/e2e/run_scenario.py"
OVERRIDE_YAML = f"""{OVERRIDE_MARKER}: this dev box runs above Elasticsearch's
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


@dataclass(frozen=True)
class UpgradePlan:
    ref: str
    work: Path
    override: str
    hold_job: bool


class LoginClock:
    """Core allows five logins a minute per address and every doctor run spends two. The
    harness marks each login it causes and waits the window out before the next doctor."""

    def __init__(self) -> None:
        self.last = 0.0

    def mark(self) -> None:
        self.last = time.monotonic()

    def wait_out(self) -> None:
        remaining = self.last + RATE_LIMIT_WINDOW - time.monotonic()
        if remaining > 0:
            print(f"+ waiting {remaining:.0f}s for the login rate limit window", flush=True)
            time.sleep(remaining)


LOGINS = LoginClock()


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
    if scenario.shared_host and _monitor_has_own_port(values):
        values["NEOPS_MONITOR_PORT"] = str(ports.monitor)
        values["NEOPS_WORKFLOWS_URL"] = _at(values["NEOPS_WORKFLOWS_URL"], scheme, ports.monitor)
    if scenario.keycloak:
        values["NEOPS_KEYCLOAK_PORT"] = str(ports.keycloak)
    if scenario.runs_grafana:
        values["NEOPS_GRAFANA_PORT"] = str(ports.grafana)


def _monitor_has_own_port(values: dict[str, str]) -> bool:
    """The rewrite above put every URL on the public port, so the port form of the shared-host
    example now reads as the web origin: the example's own NEOPS_MONITOR_PORT tells them apart."""
    return "NEOPS_MONITOR_PORT" in values


def use_self_signed_tls(values: dict[str, str], scenario: Scenario) -> None:
    """No run needs an operator-provided certificate: every tls-files example is switched over."""
    if scenario.tls != "files":
        return
    values["NEOPS_TLS_SELF_SIGNED"] = "true"
    values["NEOPS_TLS_CERT_FILE"] = "./data/secrets/tls/cert.pem"
    values["NEOPS_TLS_KEY_FILE"] = "./data/secrets/tls/key.pem"


def scenario_of(values: dict[str, str]) -> Scenario:
    return Scenario.from_env(values)


def parse_extra_env(assignments: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for assignment in assignments:
        if "=" not in assignment:
            raise SystemExit(f"--extra-env takes KEY=VALUE, got {assignment!r}")
        key, value = assignment.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def build_env(example: Path, name: str, ports: Ports, extra: dict[str, str]) -> dict[str, str]:
    values = read_example(example)
    scenario = scenario_of(values)
    fill_secrets(values)
    use_self_signed_tls(values, scenario)
    if scenario.proxy == "expose":
        apply_expose_ports(values, ports)
    else:
        apply_traefik_ports(values, scenario, ports)
    values["COMPOSE_FILE"] = values["COMPOSE_FILE"] + ":" + OVERRIDE_FILE
    values["COMPOSE_PROJECT_NAME"] = f"{E2E_PROJECT_PREFIX}{name}-{ports.base}"
    values.update(extra)
    if not values["COMPOSE_PROJECT_NAME"].startswith(E2E_PROJECT_PREFIX):
        raise SystemExit(f"refusing: --extra-env sets COMPOSE_PROJECT_NAME outside {E2E_PROJECT_PREFIX}*")
    return values


def example_path(scenario: str) -> Path:
    """A name from examples/ or tests/e2e/examples/, or the path of an .env file."""
    if scenario.endswith(".env"):
        path = Path(scenario)
        return path if path.is_absolute() else REPO / path
    shipped = REPO / "examples" / f"{scenario}.env"
    return shipped if shipped.exists() else E2E_EXAMPLES / f"{scenario}.env"


def pop_overlays(values: dict[str, str]) -> list[str]:
    """The harness-only key naming test overlays; it never reaches the clone's .env."""
    return [path for path in values.pop(HARNESS_OVERLAYS, "").split(":") if path]


def merge(base: dict, overlay: dict) -> dict:
    """Compose's own rule for mappings: keys merge, anything else is replaced."""
    out = dict(base)
    for key, value in overlay.items():
        out[key] = (
            merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
        )
    return out


def override_text(overlays: list[str]) -> str:
    """The clone's compose.override.yaml: the harness's own fragment plus each test overlay.
    `check` refuses an unshipped file in COMPOSE_FILE, so a test-only overlay reaches Compose
    through the one file meant for local additions; its relative paths resolve from the clone root."""
    if not overlays:
        return OVERRIDE_YAML
    merged = yaml.safe_load(OVERRIDE_YAML)
    for overlay in overlays:
        merged = merge(merged, yaml.safe_load((REPO / overlay).read_text()))
    header = "".join(line for line in OVERRIDE_YAML.splitlines(keepends=True) if line.startswith("#"))
    return header + f"# Merged in: {', '.join(overlays)}\n" + yaml.safe_dump(merged, sort_keys=False)


def _commit_of(rev: str) -> str:
    return sh(["git", "rev-parse", f"{rev}^{{commit}}"], cwd=REPO, capture=True).stdout.strip()


def _check_out(clone: Path, commit: str, override: str) -> None:
    sh(["git", "fetch", "-q", str(REPO), commit], cwd=clone)
    sh(["git", "checkout", "-q", "--detach", "--force", commit], cwd=clone)
    (clone / OVERRIDE_FILE).write_text(override)


def prepare_clone(
    work: Path, values: dict[str, str], rev: str = "HEAD", override: str = OVERRIDE_YAML
) -> Path:
    clone = clone_dir(work)
    work.mkdir(parents=True, exist_ok=True)
    if not clone.exists():
        sh(["git", "clone", "-q", str(REPO), str(clone)], cwd=work)
    _check_out(clone, _commit_of(rev), override)
    (clone / ".env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return clone


def move_to_head(clone: Path, override: str = OVERRIDE_YAML) -> None:
    """What `git pull` does to an operator's checkout: .env and data/ stay as they are."""
    _check_out(clone, _commit_of("HEAD"), override)


def probe_names(clone: Path, status: str, *extra: str) -> list[str]:
    """doctor prints `FAIL` or `WARN`, then the name padded to NAME_WIDTH, then the detail."""
    out = neops(clone, "doctor", "--connect", CONNECT, "--insecure", *extra, check=False, capture=True)
    print(out.stdout + out.stderr, flush=True)
    start = len(status) + 1
    return [
        line[start : start + NAME_WIDTH].strip()
        for line in out.stdout.splitlines()
        if line.startswith(status)
    ]


def failing_probe_names(clone: Path, *extra: str) -> list[str]:
    return probe_names(clone, "FAIL", *extra)


def deny_warnings_ok(warned: list[str], older_release: bool) -> bool:
    """A release before the log route probes only the job routes, so its doctor warns once."""
    if older_release:
        return DENY_PROBE in warned and set(warned) <= {DENY_PROBE, DENY_LOG_PROBE}
    return warned == [DENY_PROBE, DENY_LOG_PROBE]


def install(report: Report, clone: Path, expose: bool, label: str, older_release: bool = False) -> bool:
    """In expose mode nothing denies the worker routes until the operator's own proxy is in
    front, so doctor warns there instead of failing and the install finishes either way."""
    code = neops(clone, "install", "--connect", CONNECT, "--insecure", check=False).returncode
    LOGINS.mark()
    if not report.add(code == 0, f"{label} succeeded"):
        return False
    if not expose:
        return True
    warned = probe_names(clone, "WARN")
    LOGINS.mark()
    return report.add(
        deny_warnings_ok(warned, older_release),
        f"{label}: the worker API deny probes are the only warnings (got {warned})",
    )


def assert_admin_login(report: Report, values: dict[str, str], http: Http) -> str | None:
    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    result = login(cms, http, values.get("NEOPS_ADMIN_USER", "neops"), values["NEOPS_ADMIN_PASSWORD"])
    LOGINS.mark()
    report.add(bool(result.token), f"admin login returns an access token ({result.error})")
    return result.token


def assert_admin_manages_entities(report: Report, values: dict[str, str], http: Http, token: str) -> None:
    """Create, read back and delete a device group as the admin.

    This is what the seeded admin role buys: core gates every entity write on a role, and a
    Django superuser without one answers "User is not allowed to create a group." Logging in
    successfully does not prove the account can use the product; this does.
    """
    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    try:
        upserted = gql(http, cms, token, GROUP_UPSERT, {"n": GROUP_NAME})
        group = upserted["deviceGroupUpsert"]["deviceGroup"]
        report.add(bool(group["id"]), f"the admin creates a device group (id {group['id']})")
        found = gql(http, cms, token, GROUP_READ, {"n": GROUP_NAME})["groups"]["results"]
        report.add([g["id"] for g in found] == [group["id"]], f"the admin reads it back ({found})")
        gql(http, cms, token, GROUP_DELETE, {"id": group["id"]})
        left = gql(http, cms, token, GROUP_READ, {"n": GROUP_NAME})["groups"]["results"]
        report.add(left == [], f"the admin deletes it again ({left})")
    except Exception as exc:
        report.add(False, f"the admin creates, reads and deletes a device group ({exc})")


def assert_plain_http_admin(report: Report, values: dict[str, str], http: Http) -> None:
    """Core defaults both cookie flags to True outside DEBUG, which makes the admin site
    unusable over plain http: the browser drops the cookies and the login is rejected.
    The http scenarios turn them off in generated/cms.env and nothing else exercises that."""
    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    status, headers, body = http.fetch(cms, "/admin/login/")
    report.add(status == 200, f"the admin login page answers over http ({status})")
    hsts = headers.get("Strict-Transport-Security")
    report.add(hsts is None, f"no HSTS header is sent over http ({hsts!r})")
    cookies = headers.get_all("Set-Cookie") or []
    csrf = next((c for c in cookies if c.startswith("csrftoken=")), None)
    ok = csrf is not None and "Secure" not in csrf
    if not report.add(ok, f"the csrftoken cookie is set and is not Secure ({cookies})"):
        return
    field = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    if not report.add(field is not None, "the login form carries a csrfmiddlewaretoken"):
        return
    form = urlencode(
        {
            "csrfmiddlewaretoken": field.group(1),
            "username": values.get("NEOPS_ADMIN_USER", "neops"),
            "password": values["NEOPS_ADMIN_PASSWORD"],
            "next": "/admin/",
        }
    )
    status, headers, _ = http.fetch(
        cms,
        "/admin/login/",
        method="POST",
        body=form,
        headers={"Cookie": csrf.split(";", 1)[0]},
        content_type="application/x-www-form-urlencoded",
    )
    LOGINS.mark()
    location = headers.get("Location") or ""
    report.add(
        status == 302 and location.endswith("/admin/"),
        f"the admin login form is accepted over http ({status} -> {location})",
    )
    session = next((c for c in headers.get_all("Set-Cookie") or [] if c.startswith("sessionid=")), None)
    report.add(
        session is not None and "Secure" not in session, f"the session cookie is not Secure ({session})"
    )


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
    LOGINS.wait_out()
    install(report, clone, expose, "second install")
    after = api_key_count(clone)
    report.add(before == after == 1, f"one API key before and after the second install ({before} -> {after})")


def backup_archives(clone: Path) -> list[Path]:
    """backups/ also holds migrate's pre-migrate snapshots; only ARCHIVE_NAME names a backup."""
    return sorted(p for p in (clone / "backups").iterdir() if p.is_dir() and ARCHIVE_NAME.fullmatch(p.name))


def assert_backup(report: Report, clone: Path) -> None:
    neops(clone, "backup")
    archives = backup_archives(clone)
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


def assert_cms_task_containers(report: Report, clone: Path, expected: bool) -> None:
    """The Celery pair is opt-in: running and healthy under the profile, absent without it."""
    rows = {row.get("Service"): row for row in Compose(clone).ps()}
    for name in CMS_TASK_SERVICES:
        row = rows.get(name)
        if not expected:
            report.add(row is None, f"{name} does not exist without the cms-tasks profile")
            continue
        state = f"{row.get('State')} {row.get('Health', '')}".strip() if row else "absent"
        healthy = row is not None and row["State"] == "running" and row.get("Health", "") in ("", "healthy")
        report.add(healthy, f"{name} runs under the cms-tasks profile ({state})")


def wait_for_execution(http: Http, cms: PublicUrl, token: str, execution_id: str) -> tuple[str, str]:
    """The execution's final state and task log, or its last seen state once the wait runs out."""
    deadline = time.monotonic() + EXECUTION_TIMEOUT
    state, log = "?", ""
    while time.monotonic() < deadline:
        rows = gql(http, cms, token, EXECUTION_STATE, {"id": execution_id})["executions"]["results"]
        state, log = (rows[0]["state"], rows[0].get("taskLog") or "") if rows else ("missing", "")
        if state in EXECUTION_SETTLED:
            break
        time.sleep(3)
    return state, log


def device_facts(http: Http, cms: PublicUrl, token: str, device_id: str) -> dict:
    rows = gql(http, cms, token, DEVICE_FACTS, {"id": device_id})["devices"]["results"]
    return json.loads(rows[0]["facts"] or "{}") if rows else {}


def delete_quietly(http: Http, cms: PublicUrl, token: str, query: str, object_id: str | None) -> None:
    if object_id is None:
        return
    try:
        gql(http, cms, token, query, {"id": object_id})
    except Exception as exc:
        print(f"cleanup failed for {object_id}: {exc}", flush=True)


def create_legacy_device(http: Http, cms: PublicUrl, token: str) -> str:
    """Nornir's inventory holds only devices whose platform carries a nornir library key, so a
    device without one is silently skipped (0 hosts selected) and the task writes nothing.
    Core seeds the platforms at install; Linux Generic is one with that key."""
    platforms = gql(http, cms, token, PLATFORM_READ, {"n": LEGACY_PLATFORM})["platforms"]["results"]
    if not platforms:
        raise RuntimeError(f"platform {LEGACY_PLATFORM!r} is not seeded")
    variables = {"h": LEGACY_DEVICE, "ip": LEGACY_DEVICE_IP, "p": platforms[0]["id"]}
    return gql(http, cms, token, DEVICE_UPSERT, variables)["deviceUpsert"]["device"]["id"]


def assert_legacy_task_updates_facts(report: Report, values: dict[str, str], http: Http, token: str) -> None:
    """A Neops task run end to end on the 1.0 path: the CMS queues the execution on Redis,
    cms-worker runs the provider and writes the fact back to the device. Without the profile
    the execution stays PENDING, which is how the profile's absence would show."""
    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    device_id = task_id = None
    try:
        device_id = create_legacy_device(http, cms, token)
        task_args = {"n": LEGACY_TASK, "p": STATIC_FACTS_PROVIDER, "k": json.dumps(STATIC_FACTS_KWARGS)}
        task_id = gql(http, cms, token, TASK_UPSERT, task_args)["neopsTaskUpsert"]["neopsTask"]["id"]
        started = gql(http, cms, token, TASK_EXECUTE, {"id": task_id, "on": [device_id]})
        execution_id = started["executeNeopsTask"]["executions"][0]["id"]
        state, log = wait_for_execution(http, cms, token, execution_id)
        report.add(state == "SUCCESSFUL", f"cms-worker runs the task to completion ({state}: {log[-300:]!r})")
        facts = device_facts(http, cms, token, device_id)
        written = (facts.get(LEGACY_FACTS_KEY) or {}).get("hostname") == LEGACY_DEVICE
        report.add(written, f"the task wrote its fact on the device ({facts})")
    except Exception as exc:
        report.add(False, f"a Neops task updates a device fact through cms-worker ({exc})")
    finally:
        delete_quietly(http, cms, token, TASK_DELETE, task_id)
        delete_quietly(http, cms, token, DEVICE_DELETE, device_id)


def assert_bare_admin_redirects(report: Report, values: dict[str, str], http: Http) -> None:
    """Core answers the bare `/admin` with a 500 (neops-core #2276); the rendered Traefik
    configuration must issue the slash redirect in its place, in both routing modes."""
    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    status, headers, _ = http.fetch(cms, "/admin")
    location = headers.get("Location", "")
    report.add(
        status in (301, 308) and location == f"{cms}/admin/",
        f"the bare /admin redirects to /admin/ ({status} -> {location!r})",
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
    assert_monitor_routing(report, values, http)


def assert_monitor_routing(report: Report, values: dict[str, str], http: Http) -> None:
    """Under a path of the web origin the monitor's HTML must reference its assets under
    that prefix, or the browser fetches them from the web client's catch-all instead."""
    monitor = PublicUrl.parse(values["NEOPS_WORKFLOWS_URL"])
    web = PublicUrl.parse(values["NEOPS_WEB_URL"])
    status, html = http.request(monitor, "/")
    report.add(
        status == 200 and "<html" in html.lower(), f"the monitor index answers under {monitor} ({status})"
    )
    if (
        monitor_placement({"NEOPS_WEB_URL": web, "NEOPS_WORKFLOWS_URL": monitor})
        is not MonitorPlacement.WEB_PATH
    ):
        return
    status, config = http.request(monitor, "/config.js")
    report.add(
        status == 200 and f'basePath: "{monitor.path}"' in config,
        f"the monitor config under {monitor} carries basePath {monitor.path!r} ({status})",
    )
    report.add(
        f'src="{monitor.path}/config.js"' in html, f"the monitor HTML loads config.js under {monitor.path}"
    )


def assert_metrics_containers(report: Report, clone: Path) -> None:
    rows = {r.get("Service"): r for r in Compose(clone).ps()}
    for name in METRICS_SERVICES:
        row = rows.get(name, {})
        state, health = row.get("State", "absent"), row.get("Health", "")
        ok = state == "running" and health in ("", "healthy")
        report.add(ok, f"{name} is running ({' '.join(filter(None, (state, health)))})")


def assert_grafana_health(report: Report, values: dict[str, str], http: Http) -> None:
    grafana = PublicUrl.parse(values["NEOPS_GRAFANA_URL"])
    try:
        status, text = http.request(grafana, "/api/health")
        database = json.loads(text).get("database")
    except Exception as exc:
        report.add(False, f"grafana {grafana}/api/health: {exc}")
        return
    report.add(
        status == 200 and database == "ok", f"grafana answers /api/health ({status}, database {database})"
    )


def scrape_targets(clone: Path, timeout: float = 120.0) -> list[dict]:
    """VictoriaMetrics publishes no host port, so it is asked from inside its own container.

    The first scrape of a target is up to one interval away, so a fresh stack reports
    targets with no verdict yet; wait for one rather than calling that a failure.
    """
    deadline = time.monotonic() + timeout
    while True:
        out = Compose(clone).exec("victoriametrics", "wget", "-qO-", VM_TARGETS_URL)
        targets = json.loads(out)["data"]["activeTargets"]
        decided = targets and all(t.get("health") in ("up", "down") for t in targets)
        if decided or time.monotonic() > deadline:
            return targets
        time.sleep(5)


def assert_scrape_targets(report: Report, clone: Path) -> None:
    try:
        targets = scrape_targets(clone)
    except Exception as exc:
        report.add(False, f"victoriametrics {VM_TARGETS_URL}: {exc}")
        return
    jobs = {t["labels"].get("job") for t in targets}
    report.add(jobs == SCRAPE_JOBS, f"every scrape job is configured (missing {sorted(SCRAPE_JOBS - jobs)})")
    down = [
        f"{t['labels'].get('job')} {t.get('scrapeUrl')}: {t.get('lastError') or t.get('health')}"
        for t in targets
        if t.get("health") != "up"
    ]
    report.add(not down, f"all {len(targets)} scrape targets are up ({'; '.join(down) or 'none down'})")


def assert_metrics(report: Report, clone: Path, values: dict[str, str], http: Http) -> None:
    assert_metrics_containers(report, clone)
    assert_grafana_health(report, values, http)
    assert_scrape_targets(report, clone)


def assert_store_running(report: Report, clone: Path, store: logrecords.LogStore) -> None:
    """Running, not healthy: neither store image can run a healthcheck."""
    rows = {r.get("Service"): r for r in Compose(clone).ps()}
    state = rows.get(store.service, {}).get("State", "absent")
    report.add(state == "running", f"{store.service} is running ({state})")


def assert_sole_store(report: Report, clone: Path, store: logrecords.LogStore) -> None:
    """A Loki deployment must not also run VictoriaLogs, nor the other way round."""
    others = set(logrecords.other_store_services(store))
    found = sorted(r["Name"] for r in Compose(clone).ps() if r.get("Service") in others)
    report.add(not found, f"no container of another log store runs beside {store.service} ({found})")


def assert_log_store_isolation(report: Report, clone: Path, store: logrecords.LogStore) -> None:
    """The engine reaches the store; the worker, where function-block code runs, must not."""
    report.add(
        inside.engine_reaches(clone, store.ready_url),
        f"{store.service} answers {store.ready_path} to the engine",
    )
    isolated, why = inside.worker_isolated_from(clone, store.ready_url)
    report.add(isolated, f"{store.service} is unreachable from the worker container ({why})")


def grafana_datasource_types(clone: Path, values: dict[str, str]) -> list[str]:
    auth = f"admin:{values['NEOPS_GRAFANA_ADMIN_PASSWORD']}"
    out = Compose(clone).exec("grafana", "curl", "-fsS", "-u", auth, GRAFANA_DATASOURCES)
    return [d.get("type") for d in json.loads(out)]


def assert_grafana_has_the_logs_datasource(
    report: Report, clone: Path, values: dict[str, str], store: logrecords.LogStore
) -> None:
    types = grafana_datasource_types(clone, values)
    report.add(store.datasource_type in types, f"grafana provisions the {store.backend} datasource ({types})")


def assert_engine_log_store_ok(report: Report, deployment: workflows.Deployment) -> None:
    logs = workflows.engine_logs_status(deployment)
    report.add(logs.get("state") == "ok", f"the engine reports its log store ok ({logs})")


def assert_logs_not_queryable(
    report: Report, deployment: workflows.Deployment, engine: workflows.Engine
) -> None:
    logs = workflows.engine_logs_status(deployment)
    report.add(logs.get("queryable") is False, f"the engine reports no queryable log store ({logs})")
    status, payload = workflows.run_logs(engine, str(uuid.uuid4()))
    report.add(
        status == 503 and NOT_CONFIGURED in json.dumps(payload),
        f"a log read answers 503 {NOT_CONFIGURED} ({status}: {str(payload)[:160]})",
    )


@dataclass(frozen=True)
class RunPage:
    run: workflows.Run
    lines: logrecords.RunLines
    items: list[dict]


def read_run_page(deployment: workflows.Deployment, engine: workflows.Engine) -> RunPage:
    run = workflows.run_once(engine, workflows.prepare(deployment, engine))
    lines, items = workflows.run_lines(engine, run.execution_id)
    return RunPage(run, lines, items)


def assert_run_lines(report: Report, page: RunPage) -> None:
    """One workflow run end to end, read back through the run's log route: the worker's lines
    arrive there with the engine's and core's, which is what an operator reads first."""
    run, lines = page.run, page.lines
    report.add(
        run.succeeded,
        f"a {workflows.SAVE_FACT_FB} run completes ({run.execution_id}: {run.status}, {run.seconds:.0f}s)",
    )
    report.add(
        not lines.problems(),
        f"GET /workflow-execution/:id/logs holds the run's engine and worker lines and core's audit "
        f"line of the unlock, and no other run's ({lines}; {', '.join(lines.problems()) or 'complete'})",
    )


def assert_no_ingest_keys(report: Report, page: RunPage, store: logrecords.LogStore) -> None:
    problems = logrecords.ingest_key_problems(page.items, store.ingest_keys)
    report.add(
        bool(page.items) and not problems,
        f"no record of the run carries the keys {store.backend} adds at ingestion "
        f"({len(page.items)} records; {', '.join(problems) or 'none'})",
    )


def assert_worker_lines_round_trip(report: Report, engine: workflows.Engine) -> None:
    """Worker -> engine -> store -> engine read API: the worker's startup lines are pushed to
    POST /workers/:uuid/logs and must come back from GET /workers/:uuid/logs. The admin role
    carries worker:read through cms-init's admin profile, so a 403 is a failure, not a skip."""
    status, worker = workflows.online_worker(engine)
    report.add(
        status == 200, f"GET /workers answers the admin 200: the admin role holds worker:read ({status})"
    )
    if not report.add(worker is not None, "an online worker to read the log lines of"):
        return
    status, payload = engine.call("GET", f"/workers/{worker}/logs?limit=200")
    items = payload.get("items", []) if status == 200 and isinstance(payload, dict) else []
    services = {item.get("service") for item in items}
    report.add(
        status == 200 and logrecords.WORKER_SERVICE in services,
        f"GET /workers/{worker}/logs returns the worker's lines ({status}, {len(items)} lines, {services})",
    )


def log_assertions(
    report: Report, clone: Path, values: dict[str, str], http: Http
) -> dict[str, Callable[[], None]]:
    store = logrecords.store_of(values)
    deployment = workflows.Deployment(clone, values, http)
    engine = workflows.Engine(deployment, lambda: workflows.admin_token(deployment))
    page = functools.cache(lambda: read_run_page(deployment, engine))
    return {
        "not-queryable": lambda: assert_logs_not_queryable(report, deployment, engine),
        "store-running": lambda: assert_store_running(report, clone, store),
        "sole-store": lambda: assert_sole_store(report, clone, store),
        "store-isolated": lambda: assert_log_store_isolation(report, clone, store),
        "grafana-datasource": lambda: assert_grafana_has_the_logs_datasource(report, clone, values, store),
        "engine-store-ok": lambda: assert_engine_log_store_ok(report, deployment),
        "run-lines": lambda: assert_run_lines(report, page()),
        "no-ingest-keys": lambda: assert_no_ingest_keys(report, page(), store),
        "worker-lines": lambda: assert_worker_lines_round_trip(report, engine),
    }


def assert_logs(report: Report, clone: Path, values: dict[str, str], http: Http) -> None:
    """The checks follow the store this deployment writes to: VictoriaLogs, Loki or none."""
    checks = log_assertions(report, clone, values, http)
    for name in logrecords.log_checks(values):
        try:
            checks[name]()
        except Exception as exc:  # one broken check must not hide the others
            report.add(False, f"log check {name!r} raised {type(exc).__name__}: {exc}")


def run_assertions(report: Report, clone: Path, values: dict[str, str], scenario: Scenario) -> None:
    http = Http(connect=CONNECT, insecure=True)
    token = None
    if scenario.oidc:
        assert_oidc_seeded(report, values, http)
    else:
        token = assert_admin_login(report, values, http)
        if token:
            assert_admin_manages_entities(report, values, http, token)
        if token and scenario.cms_tasks:
            assert_legacy_task_updates_facts(report, values, http, token)
        if PublicUrl.parse(values["NEOPS_CMS_URL"]).scheme == "http":
            assert_plain_http_admin(report, values, http)
    if scenario.proxy == "traefik":
        assert_bare_admin_redirects(report, values, http)
    if scenario.shared_host:
        assert_shared_host_routing(report, values, http)
    if scenario.metrics:
        assert_metrics(report, clone, values, http)
    assert_worker(report, clone)
    assert_logs(report, clone, values, http)
    assert_cms_task_containers(report, clone, scenario.cms_tasks)
    assert_backup(report, clone)
    assert_idempotent(report, clone, scenario.proxy == "expose")


def old_example(ref: str, name: str, work: Path) -> Path:
    """The example as the older release shipped it: that is the .env an operator has."""
    text = sh(["git", "show", f"{ref}:examples/{name}.env"], cwd=REPO, capture=True).stdout
    path = work / f"{name}.upgrade-from.env"
    work.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def grafana_dashboards(clone: Path, values: dict[str, str]) -> list[str]:
    auth = f"admin:{values['NEOPS_GRAFANA_ADMIN_PASSWORD']}"
    out = Compose(clone).exec("grafana", "curl", "-fsS", "-u", auth, GRAFANA_DASHBOARDS)
    return sorted(d.get("title", "") for d in json.loads(out))


def expected_upgrade_warnings(scenario: Scenario) -> list[str]:
    """What `check` warns about on the pre-upgrade .env: a metrics install lacks
    compose.grafana.yaml until 0003 adds it; every other shape upgrades without a warning."""
    return ["compose.grafana.yaml"] if scenario.metrics and not scenario.grafana else []


def warnings_match(warned: list[str], expected: list[str]) -> bool:
    if len(warned) != len(expected):
        return False
    return all(needle in line for needle, line in zip(expected, warned, strict=True))


def expected_files_after_upgrade(files: tuple[str, ...]) -> tuple[str, ...]:
    """The oracle for 0003's COMPOSE_FILE rewrite: Grafana goes in just before metrics."""
    if "compose.metrics.yaml" not in files or "compose.grafana.yaml" in files:
        return files
    at = files.index("compose.metrics.yaml")
    return (*files[:at], "compose.grafana.yaml", *files[at:])


def assert_check_warnings(report: Report, clone: Path, expected: list[str]) -> None:
    out = neops(clone, "check", "--no-images", check=False, capture=True)
    print(out.stdout + out.stderr, flush=True)
    warned = [line for line in out.stdout.splitlines() if line.startswith("WARNING:")]
    report.add(out.returncode == 0, f"check passes on the pre-upgrade .env (exit {out.returncode})")
    report.add(warnings_match(warned, expected), f"check warns about {expected or 'nothing'} ({warned})")


def assert_env_migrated(report: Report, clone: Path, files_before: tuple[str, ...]) -> None:
    applied = State.load(clone / "data" / ".neops" / "state.json").applied_names
    report.add(GRAFANA_AND_LOGS_MIGRATION in applied, f"{GRAFANA_AND_LOGS_MIGRATION} is recorded as applied")
    env = Env(clone / ".env")
    files = Scenario.from_env(env).files
    expected = expected_files_after_upgrade(files_before)
    report.add(files == expected, f"COMPOSE_FILE is {':'.join(expected)} ({':'.join(files)})")
    backend = env.get(LOG_BACKEND)
    report.add(
        backend == logrecords.DB_BACKEND,
        f"{LOG_BACKEND} is set to db, what the CMS used before ({backend!r})",
    )
    retention = env.get("NEOPS_LOG_RETENTION")
    report.add(retention == "90d", f"NEOPS_LOG_RETENTION carries the old 90-day horizon ({retention!r})")


def password_login(deployment: workflows.Deployment, values: dict[str, str]) -> Callable[[], str]:
    """The old core may predate the CMS-issued token, so a hook on the old install logs in."""
    LOGINS.wait_out()

    def token() -> str:
        LOGINS.mark()
        result = login(deployment.cms, deployment.http, deployment.admin_user, values["NEOPS_ADMIN_PASSWORD"])
        if not result.token:
            raise upgrade_hook.HookError(result.error)
        return result.token

    return token


def hold_job_before_upgrade(
    report: Report, clone: Path, values: dict[str, str], work: Path
) -> upgrade_hook.HeldJob | None:
    """C12b, first half."""
    deployment = workflows.Deployment(clone, values, Http(connect=CONNECT, insecure=True))
    try:
        engine = workflows.Engine(deployment, password_login(deployment, values))
        held = upgrade_hook.hold_job(deployment, engine, work)
    except Exception as exc:
        report.add(False, f"C12b: hold a job on the old install as its worker ({exc}); C12b stays open")
        return None
    report.add(True, f"C12b: job {held.job_id} of run {held.execution_id} polled for worker {held.worker_id}")
    return held


def assert_held_job_refused(
    report: Report, clone: Path, values: dict[str, str], held: upgrade_hook.HeldJob
) -> None:
    """C12b, second half: the lease-less job after the upgrade."""
    deployment = workflows.Deployment(clone, values, Http(connect=CONNECT, insecure=True))
    status, body = upgrade_hook.report_without_lease(deployment, held)
    report.add(
        upgrade_hook.refused_for_missing_lease(status, body),
        f"C12b: a result for the job polled before the upgrade is refused 403 "
        f"{upgrade_hook.LEASE_MISSING} ({status}: {body[:160]})",
    )
    engine = workflows.Engine(deployment, lambda: workflows.admin_token(deployment))
    final = upgrade_hook.final_status(engine, held)
    report.add(
        final in workflows.SETTLED and final not in workflows.SUCCEEDED,
        f"C12b: the run whose job was held ends failed ({final})",
    )


def write_survivor(report: Report, clone: Path, values: dict[str, str]) -> bool:
    """A device group created on the old install, read back after the upgrade."""
    deployment = workflows.Deployment(clone, values, Http(connect=CONNECT, insecure=True))
    token = password_login(deployment, values)()
    created = gql(deployment.http, deployment.cms, token, GROUP_UPSERT, {"n": SURVIVOR_GROUP})
    return report.add(
        bool(created["deviceGroupUpsert"]["deviceGroup"]["id"]),
        f"device group {SURVIVOR_GROUP} created on the old install",
    )


def assert_survivor(report: Report, clone: Path, values: dict[str, str]) -> None:
    deployment = workflows.Deployment(clone, values, Http(connect=CONNECT, insecure=True))
    token = workflows.admin_token(deployment)
    found = gql(deployment.http, deployment.cms, token, GROUP_READ, {"n": SURVIVOR_GROUP})["groups"][
        "results"
    ]
    report.add(len(found) == 1, f"device group {SURVIVOR_GROUP} survived the upgrade ({found})")


def backup_before_upgrade(report: Report, clone: Path) -> None:
    """What an operator takes before `git pull`, and what a rollback restores."""
    neops(clone, "backup")
    archives = backup_archives(clone)
    report.add(
        bool(archives) and (archives[-1] / "cms.dump").exists() and (archives[-1] / "engine.dump").exists(),
        f"backup of the old install before the upgrade ({archives[-1].name if archives else 'none'})",
    )


def run_upgrade(report: Report, clone: Path, values: dict[str, str], plan: UpgradePlan) -> None:
    """`git pull && ./neops up` on an install of `plan.ref`, and nothing else."""
    scenario = scenario_of(values)
    if not install(report, clone, scenario.proxy == "expose", f"install of {plan.ref}", older_release=True):
        return
    before = grafana_dashboards(clone, values) if scenario.runs_grafana else []
    held = hold_job_before_upgrade(report, clone, values, plan.work) if plan.hold_job else None
    survivor = write_survivor(report, clone, values)
    backup_before_upgrade(report, clone)
    move_to_head(clone, plan.override)
    assert_check_warnings(report, clone, expected_upgrade_warnings(scenario))
    code = neops(clone, "up", "--connect", CONNECT, "--insecure", check=False).returncode
    if not report.add(
        code == 0, f"up after the upgrade from {plan.ref} succeeded, doctor green (exit {code})"
    ):
        return
    assert_env_migrated(report, clone, scenario.files)
    if survivor:
        assert_survivor(report, clone, values)
    if held:
        assert_held_job_refused(report, clone, values, held)
    if not scenario.runs_grafana:
        return
    after = grafana_dashboards(clone, values)
    report.add(
        bool(before) and set(before) <= set(after),
        f"grafana kept its dashboards across the upgrade ({len(before)} before, {len(after)} after)",
    )


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


def clone_dir(work: Path) -> Path:
    return work / "repo"


def clone_refusal(clone: Path, environ: Mapping[str, str]) -> str | None:
    """Why a harness step must not act on `clone`, or None for a stack this script left behind.
    Chaos and the proxy check stop, swap and rewrite services, so anything else, the live
    checkout first of all, is refused."""
    if refusal := shell_compose_refusal(environ, clone):
        return refusal
    env = Env(clone / ".env")
    if not env.exists:
        return f"{clone} does not look like an installed deployment: no .env"
    project = env.get("COMPOSE_PROJECT_NAME")
    if not project.startswith(E2E_PROJECT_PREFIX):
        return f"refusing: COMPOSE_PROJECT_NAME {project!r} is no harness project ({E2E_PROJECT_PREFIX}*)"
    override = clone / OVERRIDE_FILE
    if not override.is_file() or not override.read_text().startswith(OVERRIDE_MARKER):
        return f"refusing: {override} was not written by tests/e2e/run_scenario.py"
    return None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", help="a name from examples/ or tests/e2e/examples/, or an .env path")
    ap.add_argument("--port-base", type=int, default=18000)
    ap.add_argument("--keep", action="store_true", help="leave the stack running and the clone in place")
    ap.add_argument("--workdir", type=Path)
    ap.add_argument(
        "--extra-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override one .env entry, after everything the harness derives (repeatable)",
    )
    ap.add_argument(
        "--upgrade-from",
        metavar="REF",
        help="install the scenario as release REF shipped it, then git pull to this checkout and up",
    )
    ap.add_argument(
        "--hold-job",
        action="store_true",
        help="with --upgrade-from: before the pull, hold a polled job on the old install, and check "
        "after the upgrade that its lease-less result is refused (chaos C12b)",
    )
    args = ap.parse_args(argv)
    if args.hold_job and not args.upgrade_from:
        ap.error("--hold-job needs --upgrade-from")
    return args


def chosen_example(scenario: str) -> Path:
    example = example_path(scenario)
    if not example.exists():
        raise SystemExit(f"no such example: {example}")
    if example.stem == "traefik-acme":
        raise SystemExit(
            "traefik-acme needs public DNS and port 80; it is covered by the compose-config gate"
        )
    return example


def print_summary(name: str, report: Report, elapsed: float) -> None:
    print(
        f"\n{name}: {len(report.results) - len(report.failed)}/{len(report.results)} assertions "
        f"passed in {elapsed:.0f}s",
        flush=True,
    )
    for message in report.failed:
        print(f"  FAILED: {message}", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    example = chosen_example(args.scenario)
    name = example.stem
    ports = Ports(args.port_base)
    work = args.workdir or Path(f"/tmp/neops-e2e/{name}-{ports.base}")
    if refusal := shell_compose_refusal(os.environ, clone_dir(work)):
        print(refusal, file=sys.stderr)
        return 2
    extra = parse_extra_env(args.extra_env)
    if args.upgrade_from:
        example = old_example(args.upgrade_from, name, work)
        extra = UPGRADE_ENV | extra
    values = build_env(example, name, ports, extra)
    override = override_text(pop_overlays(values))
    scenario = scenario_of(values)
    clone = prepare_clone(work, values, rev=args.upgrade_from or "HEAD", override=override)
    print(f"workdir {work}", flush=True)

    report = Report()
    started = time.monotonic()
    try:
        neops(clone, "check", "--no-images")
        if args.upgrade_from:
            run_upgrade(report, clone, values, UpgradePlan(args.upgrade_from, work, override, args.hold_job))
        elif install(report, clone, scenario.proxy == "expose", "install"):
            run_assertions(report, clone, values, scenario)
    finally:
        elapsed = time.monotonic() - started
        if args.keep:
            print(f"stack left running in {clone}", flush=True)
        else:
            neops(clone, "down", check=False)
            remove_work(work)
    print_summary(name, report, elapsed)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
