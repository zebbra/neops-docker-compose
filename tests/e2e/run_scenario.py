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
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from neops_compose.backup import ARCHIVE_NAME  # noqa: E402
from neops_compose.compose import Compose  # noqa: E402
from neops_compose.doctor import NAME_WIDTH, Http, login  # noqa: E402
from neops_compose.scenario import OVERRIDE_FILE, Scenario  # noqa: E402
from neops_compose.urls import MonitorPlacement, PublicUrl, monitor_placement  # noqa: E402

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
GROUP_UPSERT = "mutation($n:String!){deviceGroupUpsert(name:$n,title:$n){deviceGroup{id name}}}"
GROUP_READ = "query($n:String!){groups(name:$n){results{id name}}}"
GROUP_DELETE = "mutation($id:ID!){deviceGroupDelete(id:$id){deviceGroup{id}}}"
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
    if scenario.metrics:
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
    values["COMPOSE_PROJECT_NAME"] = f"neops-e2e-{name}-{ports.base}"
    values.update(extra)
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


def install(report: Report, clone: Path, expose: bool, label: str) -> bool:
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
        warned == [DENY_PROBE],
        f"{label}: the worker API deny probe is the only warning (got {warned})",
    )


def assert_admin_login(report: Report, values: dict[str, str], http: Http) -> str | None:
    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    result = login(cms, http, values.get("NEOPS_ADMIN_USER", "neops"), values["NEOPS_ADMIN_PASSWORD"])
    LOGINS.mark()
    report.add(bool(result.token), f"admin login returns an access token ({result.error})")
    return result.token


def gql(http: Http, cms: PublicUrl, token: str, query: str, variables: dict) -> dict:
    """The `data` block of one authenticated GraphQL call, or a RuntimeError naming the errors.

    Core answers a refused write with HTTP 200 and an `errors` list, so the status alone says
    nothing about whether the call did what it was asked to.
    """
    status, text = http.request(
        cms,
        "/graphql",
        method="POST",
        body=json.dumps({"query": query, "variables": variables}),
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        payload = json.loads(text)
    except ValueError:
        raise RuntimeError(f"{status}: {text[:200]}") from None
    if status != 200 or payload.get("errors"):
        raise RuntimeError(f"{status}: {json.dumps(payload.get('errors') or payload)[:200]}")
    return payload["data"]


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


def assert_backup(report: Report, clone: Path) -> None:
    """backups/ also holds migrate's pre-migrate snapshots; only ARCHIVE_NAME names a backup."""
    neops(clone, "backup")
    archives = sorted(
        p for p in (clone / "backups").iterdir() if p.is_dir() and ARCHIVE_NAME.fullmatch(p.name)
    )
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


def run_assertions(report: Report, clone: Path, values: dict[str, str], scenario: Scenario) -> None:
    http = Http(connect=CONNECT, insecure=True)
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
    assert_cms_task_containers(report, clone, scenario.cms_tasks)
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
    ap.add_argument(
        "--extra-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override one .env entry, after everything the harness derives (repeatable)",
    )
    args = ap.parse_args(argv)

    example = REPO / "examples" / f"{args.scenario}.env"
    if not example.exists():
        raise SystemExit(f"no such example: {example}")
    if args.scenario == "traefik-acme":
        raise SystemExit(
            "traefik-acme needs public DNS and port 80; it is covered by the compose-config gate"
        )

    ports = Ports(args.port_base)
    values = build_env(example, args.scenario, ports, parse_extra_env(args.extra_env))
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
