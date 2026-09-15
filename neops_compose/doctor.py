from __future__ import annotations

import http.client
import json
import socket
import ssl
from dataclasses import dataclass

from neops_compose.context import DEFAULT_ADMIN_USER, Ctx
from neops_compose.env import Env
from neops_compose.scenario import Scenario
from neops_compose.urls import PublicUrl

ONE_SHOTS = {"cms-init"}
NAME_WIDTH = 28
LOGIN_MUTATION = "mutation($u:String!,$p:String!){login(username:$u,password:$p){accessToken}}"
WORKER_PROBE = "worker registered"
DB_UNREACHABLE = "the CMS cannot reach its database (login answered: internal error)"
EXTERNAL_PROXY_DENY_HINT = "verify that your reverse proxy denies these routes (docs/40-external-proxy.md)"


@dataclass(frozen=True)
class Probe:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "fail"  # "warn" means doctor reports it and still passes


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    url: PublicUrl
    path: str
    want: int = 200
    contains: str = ""
    method: str = "GET"
    body: str | None = None


@dataclass(frozen=True)
class Login:
    token: str | None = None
    error: str = ""
    rate_limited: bool = False
    db_down: bool = False


def cms_database_is_down(text: str) -> bool:
    """Core answers the login mutation with a bare "internal error" while its database is
    unreachable. A wrong password answers a credentials message instead, so this is the one
    login response that says nothing about the credentials and everything about the CMS."""
    return "internal error" in text.lower()


class Http:
    """Minimal HTTP client that can connect to one address while sending another Host.

    Boxes without DNS for the public hostnames need that split.
    """

    def __init__(self, connect: str | None = None, insecure: bool = False, timeout: float = 15.0):
        self.connect = connect
        self.timeout = timeout
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _open(self, url: PublicUrl) -> http.client.HTTPConnection:
        target = self.connect or url.host
        if url.scheme != "https":
            return http.client.HTTPConnection(target, url.port, timeout=self.timeout)
        conn = http.client.HTTPSConnection(target, url.port, timeout=self.timeout, context=self.ctx)
        conn.sock = self.ctx.wrap_socket(
            socket.create_connection((target, url.port), self.timeout), server_hostname=url.host
        )
        return conn

    def fetch(
        self,
        url: PublicUrl,
        path: str,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, http.client.HTTPMessage, str]:
        conn = self._open(url)
        hdrs = {
            "Host": url.host if url.is_default_port else f"{url.host}:{url.port}",
            "User-Agent": "neops-doctor",
        }
        if body is not None:
            hdrs["Content-Type"] = content_type
        hdrs.update(headers or {})
        try:
            conn.request(method, url.path + path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.headers, resp.read().decode(errors="replace")
        finally:
            conn.close()

    def request(
        self,
        url: PublicUrl,
        path: str,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        status, _, text = self.fetch(url, path, method, body, headers)
        return status, text

    def cert_days_left(self, url: PublicUrl) -> int | None:
        if url.scheme != "https":
            return None
        import datetime as dt

        with socket.create_connection((self.connect or url.host, url.port), self.timeout) as raw:
            with self.ctx.wrap_socket(raw, server_hostname=url.host) as s:
                der = s.getpeercert(binary_form=True)
        from cryptography import x509

        cert = x509.load_der_x509_certificate(der)
        return (cert.not_valid_after_utc - dt.datetime.now(dt.UTC)).days


def container_probes(rows: list[dict], one_shots: set[str] = ONE_SHOTS) -> list[Probe]:
    out = []
    for row in rows:
        name, state, health = row.get("Service", "?"), row.get("State", ""), row.get("Health", "")
        if name in one_shots:
            ok = state == "exited" and int(row.get("ExitCode", 1)) == 0
            out.append(Probe(name, ok, f"{state} (exit {row.get('ExitCode', '?')})"))
        else:
            ok = state == "running" and health in ("", "healthy")
            out.append(Probe(name, ok, f"{state} {health}".strip()))
    return out


def _probe_get(http: Http, spec: ProbeSpec) -> Probe:
    try:
        status, text = http.request(spec.url, spec.path, method=spec.method, body=spec.body)
    except Exception as exc:
        return Probe(spec.name, False, f"{spec.url}{spec.path}: {exc}")
    ok = status == spec.want and (spec.contains in text)
    detail = f"{spec.url}{spec.path} -> {status}"
    if not ok:
        detail += f", expected {spec.want}" + (f" containing {spec.contains!r}" if spec.contains else "")
    return Probe(spec.name, ok, detail)


def _deny_probe(engine: PublicUrl, http: Http, severity: str = "fail") -> Probe:
    """The worker API must never answer a request that arrived over the public URL.

    In expose mode the deny belongs to a proxy the operator has not necessarily put in front
    yet, so this is a warning there: a first install must be able to finish and say so.
    """
    name = "engine worker API denied"
    hint = (
        EXTERNAL_PROXY_DENY_HINT
        if severity == "warn"
        else "the worker API is reachable from outside; your reverse proxy must deny it "
        "(see examples/external-proxy.env)"
    )
    try:
        status, _ = http.request(engine, "/blackboard/job", method="POST", body="{}")
    except Exception as exc:
        return Probe(name, False, f"{engine}/blackboard/job: {exc}", severity)
    if status == 403:
        return Probe(name, True, f"{engine}/blackboard/job -> 403")
    return Probe(name, False, f"{engine}/blackboard/job -> {status}: {hint}", severity)


def _specs(urls: dict[str, PublicUrl]) -> list[ProbeSpec]:
    web, cms, engine, monitor = (
        urls[k] for k in ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL")
    )
    specs = [
        ProbeSpec("web client", web, "/", contains="app-root"),
        ProbeSpec("cms admin", cms, "/admin/login/"),
        ProbeSpec(
            "cms graphql",
            cms,
            "/graphql",
            contains="__typename",
            method="POST",
            body='{"query":"{__typename}"}',
        ),
        ProbeSpec("engine health", engine, "/health"),
        ProbeSpec("monitor config", monitor, "/config.js", contains=str(engine)),
    ]
    if "NEOPS_KEYCLOAK_URL" in urls:
        specs.append(
            ProbeSpec(
                "keycloak realm",
                urls["NEOPS_KEYCLOAK_URL"],
                "/realms/neops/.well-known/openid-configuration",
                contains="authorization_endpoint",
            )
        )
    if "NEOPS_GRAFANA_URL" in urls:
        specs.append(ProbeSpec("grafana", urls["NEOPS_GRAFANA_URL"], "/api/health"))
    return specs


def http_probes(urls: dict[str, PublicUrl], http: Http, deny_severity: str = "fail") -> list[Probe]:
    probes = [_probe_get(http, spec) for spec in _specs(urls)]
    probes.append(_deny_probe(urls["NEOPS_ENGINE_URL"], http, deny_severity))
    return probes


def bad_login_probe(cms: PublicUrl, http: Http) -> Probe:
    body = json.dumps({"query": LOGIN_MUTATION, "variables": {"u": "neops-doctor", "p": "not-the-password"}})
    try:
        status, text = http.request(cms, "/graphql", method="POST", body=body)
    except Exception as exc:
        return Probe("cms login path", False, str(exc))
    if status >= 500:
        return Probe(
            "cms login path",
            False,
            f"login mutation answered {status}: {text[:160]} (behind an external proxy this "
            "usually means RATELIMIT_IP_META_KEY is set but X-Real-IP is not)",
        )
    if cms_database_is_down(text):
        return Probe("cms login path", False, DB_UNREACHABLE)
    return Probe("cms login path", True, f"bad credentials answered {status} (no server error)")


def login(cms: PublicUrl, http: Http, username: str, password: str) -> Login:
    body = json.dumps({"query": LOGIN_MUTATION, "variables": {"u": username, "p": password}})
    try:
        status, text = http.request(cms, "/graphql", method="POST", body=body)
    except Exception as exc:
        return Login(error=str(exc))
    try:
        return Login(token=json.loads(text)["data"]["login"]["accessToken"])
    except Exception:
        return Login(
            error=f"{status}: {text[:160]}",
            rate_limited=status == 429 or "Too many" in text,
            db_down=cms_database_is_down(text),
        )


def worker_probe(engine: PublicUrl, http: Http, admin: Login) -> Probe:
    if admin.db_down:
        return Probe(WORKER_PROBE, False, DB_UNREACHABLE)
    if admin.rate_limited:
        return Probe(WORKER_PROBE, False, "admin login refused (rate limit, 5/min): retry in a minute")
    if not admin.token:
        detail = "could not log in as the admin user to ask the engine"
        return Probe(WORKER_PROBE, False, f"{detail}: {admin.error}" if admin.error else detail)
    status, text = http.request(engine, "/workers", headers={"Authorization": f"Bearer {admin.token}"})
    if status == 403:
        return Probe(
            WORKER_PROBE,
            True,
            "admin token accepted, no worker:read permission (assign a role to check further)",
        )
    if status != 200:
        return Probe(WORKER_PROBE, False, f"GET /workers -> {status}")
    try:
        workers = json.loads(text)
        items = workers if isinstance(workers, list) else workers.get("items", workers.get("data", []))
        online = [w for w in items if str(w.get("status", w.get("state", ""))).upper() == "ONLINE"]
        return Probe(WORKER_PROBE, bool(online), f"{len(online)} online of {len(items)} workers")
    except Exception as exc:
        return Probe(WORKER_PROBE, False, f"unexpected /workers payload: {exc}")


def ratelimit_probe(cms: PublicUrl, http: Http) -> Probe:
    """Six bad logins with a different forged X-Real-IP each.

    Core limits local login to 5/min per IP: if none is refused, the proxy lets clients
    choose their own IP, because it appends to X-Real-IP instead of overwriting it.
    """
    body = json.dumps({"query": LOGIN_MUTATION, "variables": {"u": "neops-doctor", "p": "not-the-password"}})
    refused = False
    for i in range(6):
        status, text = http.request(
            cms, "/graphql", method="POST", body=body, headers={"X-Real-IP": f"203.0.113.{i + 1}"}
        )
        if status == 429 or "Too many" in text:
            refused = True
    return Probe(
        "X-Real-IP trusted from client",
        refused,
        "rate limit applied per real address"
        if refused
        else "6 forged X-Real-IP logins were all accepted: the proxy must OVERWRITE X-Real-IP",
    )


def _public_urls(env: Env, scenario: Scenario) -> dict[str, PublicUrl]:
    keys = ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL")
    urls = {k: PublicUrl.parse(env.require(k)) for k in keys}
    if scenario.keycloak:
        urls["NEOPS_KEYCLOAK_URL"] = PublicUrl.parse(env.require("NEOPS_KEYCLOAK_URL"))
    if scenario.metrics and env.is_set("NEOPS_GRAFANA_URL"):
        urls["NEOPS_GRAFANA_URL"] = PublicUrl.parse(env.get("NEOPS_GRAFANA_URL"))
    return urls


def _login_probes(ctx: Ctx, cms: PublicUrl, engine: PublicUrl, http: Http) -> list[Probe]:
    """The admin login goes first: core allows five logins a minute from one address, and
    the bad-credentials probe spends one of them."""
    if ctx.scenario.oidc:
        skipped = Probe(WORKER_PROBE, True, "skipped: OIDC deployments cannot mint an admin token")
        return [bad_login_probe(cms, http), skipped]
    user = ctx.env.get("NEOPS_ADMIN_USER", DEFAULT_ADMIN_USER)
    admin = login(cms, http, user, ctx.env.get("NEOPS_ADMIN_PASSWORD"))
    return [bad_login_probe(cms, http), worker_probe(engine, http, admin)]


def _tls_probe(http: Http, web: PublicUrl) -> Probe:
    try:
        days = http.cert_days_left(web)
    except Exception as exc:
        return Probe("tls certificate", False, str(exc))
    return Probe("tls certificate", days is not None and days > 14, f"{days} days until expiry")


def run(
    ctx: Ctx, connect: str | None = None, insecure: bool = False, probe_ratelimit: bool = False
) -> list[Probe]:
    probes = container_probes(ctx.compose.ps())
    urls = _public_urls(ctx.env, ctx.scenario)
    http = Http(connect=connect, insecure=insecure)
    probes += http_probes(urls, http, "warn" if ctx.scenario.proxy == "expose" else "fail")
    probes += _login_probes(ctx, urls["NEOPS_CMS_URL"], urls["NEOPS_ENGINE_URL"], http)
    if probe_ratelimit:
        probes.append(ratelimit_probe(urls["NEOPS_CMS_URL"], http))
    if ctx.scenario.tls:
        probes.append(_tls_probe(http, urls["NEOPS_WEB_URL"]))
    return probes


def _status(probe: Probe) -> str:
    if probe.ok:
        return "OK  "
    return "WARN" if probe.severity == "warn" else "FAIL"


def format_report(probes: list[Probe]) -> str:
    return "\n".join(f"{_status(p)} {p.name:<{NAME_WIDTH}} {p.detail}".rstrip() for p in probes)


def all_ok(probes: list[Probe]) -> bool:
    """Warnings are reported and do not fail the run: nothing else here is advisory."""
    return all(p.ok or p.severity == "warn" for p in probes)
