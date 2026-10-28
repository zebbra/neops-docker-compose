from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
from dataclasses import dataclass

from neops_compose.context import Ctx
from neops_compose.token import mint_access_token
from neops_compose.urls import BASE_URLS, PublicUrl, public_urls

ONE_SHOTS = {"cms-init"}
NAME_WIDTH = 28
LOGIN_MUTATION = "mutation($u:String!,$p:String!){login(username:$u,password:$p){accessToken}}"
OIDC_EXCHANGE_MUTATION = "mutation($c:String!){exchangeOidcCode(code:$c){accessToken}}"
# Well-formed (core's 64-character code pattern) and never issued: core rate-limits, then looks it up.
OIDC_UNKNOWN_CODE = "neops-doctor-" + "0" * 51
OIDC_UNKNOWN_CODE_ANSWER = "Invalid authorization code"
AUTH_PATH_PROBE = "cms login path"
DENY_PROBE = "engine worker API denied"
DENY_LOG_PROBE = "engine worker logs denied"
# Two @Public worker routes stand for the whole denied set: the job routes and the log route.
DENY_PATHS = {
    DENY_PROBE: "/blackboard/job",
    DENY_LOG_PROBE: "/workers/00000000-0000-0000-0000-000000000000/logs",
}
LOGS_PROBE = "engine log store"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
LOOPBACK_ONLY = (
    "not applicable: no reverse proxy in this scenario, and the engine listens on 127.0.0.1 only, "
    "so these routes are reachable from this host and from nowhere else"
)
WORKER_PROBE = "worker registered"
WORKER_POLL_SECONDS = 5
DB_UNREACHABLE = "the CMS cannot reach its database (login answered: internal error)"
EXTERNAL_PROXY_DENY_HINT = (
    "the worker API answered over the public URL. Expected until your reverse proxy is up; "
    "once it is, it must deny these routes (docs/40-external-proxy.md)"
)


@dataclass(frozen=True)
class Probe:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "fail"  # "warn" means doctor reports it and still passes
    pending: bool = False  # not ok yet, but a state that resolves itself: worth asking again


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


def _deny_probe(engine: PublicUrl, http: Http, name: str, severity: str = "fail") -> Probe:
    """The worker API must never answer a request that arrived over the public URL.

    In expose mode the deny belongs to a proxy the operator has not necessarily put in front
    yet, so this is a warning there: a first install must be able to finish and say so.
    """
    if severity == "skip":
        return Probe(name, True, LOOPBACK_ONLY)
    hint = (
        EXTERNAL_PROXY_DENY_HINT
        if severity == "warn"
        else "the worker API is reachable from outside; your reverse proxy must deny it "
        "(see examples/external-proxy.env)"
    )
    target = f"{engine}{DENY_PATHS[name]}"
    try:
        status, _ = http.request(engine, DENY_PATHS[name], method="POST", body="{}")
    except Exception as exc:
        return Probe(name, False, f"{target}: {exc}", severity)
    if status == 403:
        return Probe(name, True, f"{target} -> 403")
    return Probe(name, False, f"{target} -> {status}: {hint}", severity)


def _store_probe(logs: dict) -> Probe:
    """Judge the `logs` object of the engine's GET /version."""
    if not logs.get("queryable"):
        return Probe(
            LOGS_PROBE, False, "the engine has no log store configured (NEOPS_LOG_BACKEND, NEOPS_LOG_URL)"
        )
    state, backend = logs.get("state"), logs.get("backend")
    if state == "misconfigured":
        return Probe(LOGS_PROBE, False, f"the engine's {backend} rejects its pushes: {logs.get('lastError')}")
    if state == "open":
        detail = f"{backend} is unreachable; the engine spools lines to its database meanwhile"
        return Probe(LOGS_PROBE, False, detail, "warn")
    return Probe(LOGS_PROBE, True, f"{backend} {state}")


def logs_probe(engine: PublicUrl, http: Http) -> Probe:
    """The engine reports its log pipeline on GET /version; doctor reads it from there."""
    try:
        status, text = http.request(engine, "/version")
        logs = json.loads(text).get("logs") if status == 200 else None
    except Exception as exc:
        return Probe(LOGS_PROBE, False, f"{engine}/version: {exc}")
    if not isinstance(logs, dict):
        detail = f"{engine}/version -> {status} reports no log store (an engine before 0.44?)"
        return Probe(LOGS_PROBE, False, detail)
    return _store_probe(logs)


def _specs(urls: dict[str, PublicUrl]) -> list[ProbeSpec]:
    web, cms, engine, monitor = (urls[k] for k in BASE_URLS)
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
    probes += [_deny_probe(urls["NEOPS_ENGINE_URL"], http, name, deny_severity) for name in DENY_PATHS]
    return probes


def _rejected_credentials_body(oidc: bool) -> str:
    """Core refuses `login` under OIDC before its rate limiter and its database, so there only
    the code exchange travels the same path."""
    if oidc:
        return json.dumps({"query": OIDC_EXCHANGE_MUTATION, "variables": {"c": OIDC_UNKNOWN_CODE}})
    return json.dumps({"query": LOGIN_MUTATION, "variables": {"u": "neops-doctor", "p": "not-the-password"}})


def _oidc_auth_path_problem(text: str) -> str:
    if OIDC_UNKNOWN_CODE_ANSWER in text:
        return ""
    if "Too many" in text:
        return "OIDC code exchange refused (rate limit, 20/min): retry in a minute"
    return f"the CMS could not look up an OIDC code (is postgres-cms up?): {text[:160]}"


def bad_login_probe(cms: PublicUrl, http: Http, oidc: bool) -> Probe:
    try:
        status, text = http.request(cms, "/graphql", method="POST", body=_rejected_credentials_body(oidc))
    except Exception as exc:
        return Probe(AUTH_PATH_PROBE, False, str(exc))
    if status >= 500:
        return Probe(
            AUTH_PATH_PROBE,
            False,
            f"a rejected-credentials request answered {status}: {text[:160]} (behind an external proxy this "
            "usually means RATELIMIT_IP_META_KEY is set but X-Real-IP is not)",
        )
    problem = (
        _oidc_auth_path_problem(text) if oidc else (DB_UNREACHABLE if cms_database_is_down(text) else "")
    )
    if problem:
        return Probe(AUTH_PATH_PROBE, False, problem)
    sent = "a made-up OIDC code" if oidc else "a wrong test password"
    return Probe(
        AUTH_PATH_PROBE, True, f"doctor sent {sent} on purpose; the CMS rejected it cleanly ({status})"
    )


def login(cms: PublicUrl, http: Http, username: str, password: str) -> Login:
    body = json.dumps({"query": LOGIN_MUTATION, "variables": {"u": username, "p": password}})
    try:
        status, text = http.request(cms, "/graphql", method="POST", body=body)
    except Exception as exc:
        return Login(error=f"could not log in as the admin user: {exc}")
    try:
        return Login(token=json.loads(text)["data"]["login"]["accessToken"])
    except Exception:
        return Login(
            error=f"could not log in as the admin user: {status}: {text[:160]}",
            rate_limited=status == 429 or "Too many" in text,
            db_down=cms_database_is_down(text),
        )


def worker_probe(engine: PublicUrl, http: Http, admin: Login) -> Probe:
    if admin.db_down:
        return Probe(WORKER_PROBE, False, DB_UNREACHABLE)
    if admin.rate_limited:
        return Probe(WORKER_PROBE, False, "admin login refused (rate limit, 5/min): retry in a minute")
    if not admin.token:
        return Probe(WORKER_PROBE, False, admin.error or "no admin token to ask the engine with")
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
        detail = f"{len(online)} online of {len(items)} workers"
        return Probe(WORKER_PROBE, bool(online), detail, pending=not online)
    except Exception as exc:
        return Probe(WORKER_PROBE, False, f"unexpected /workers payload: {exc}")


def _ratelimited_request(oidc: bool) -> tuple[str, int]:
    """Core limits local login to 5/min per IP and the OIDC code exchange to 20/min. Under OIDC
    login is refused before its limiter runs, so only the exchange can show the limit."""
    return _rejected_credentials_body(oidc), 21 if oidc else 6


def ratelimit_probe(cms: PublicUrl, http: Http, oidc: bool) -> Probe:
    """One more rate-limited request than the limit allows, each with a different forged X-Real-IP.

    If none is refused, the proxy lets clients choose their own IP, because it appends to
    X-Real-IP instead of overwriting it.
    """
    body, attempts = _ratelimited_request(oidc)
    refused = False
    for i in range(attempts):
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
        else f"{attempts} forged X-Real-IP requests were all accepted: the proxy must OVERWRITE X-Real-IP",
    )


def settled_worker_probe(engine: PublicUrl, http: Http, admin: Login, grace: float) -> Probe:
    """A worker registers its function blocks one request at a time once the engine answers,
    so for a while after a start "0 online" is the honest answer and not the verdict."""
    deadline = time.monotonic() + grace
    while True:
        probe = worker_probe(engine, http, admin)
        if not probe.pending or time.monotonic() >= deadline:
            return probe
        time.sleep(WORKER_POLL_SECONDS)


def _login_probes(
    ctx: Ctx, cms: PublicUrl, engine: PublicUrl, http: Http, worker_grace: float
) -> list[Probe]:
    """The admin token goes first: without OIDC it is a login, core allows five a minute from
    one address, and the bad-credentials probe spends one of them."""
    admin = _admin_token(ctx, cms, http)
    return [
        bad_login_probe(cms, http, ctx.scenario.oidc),
        settled_worker_probe(engine, http, admin, worker_grace),
    ]


def _admin_token(ctx: Ctx, cms: PublicUrl, http: Http) -> Login:
    """Under OIDC local login is disabled, so the CMS issues the token instead of a password login."""
    if not ctx.scenario.oidc:
        return login(cms, http, ctx.admin_user, ctx.env.get("NEOPS_ADMIN_PASSWORD"))
    try:
        return Login(token=mint_access_token(ctx.compose, ctx.admin_user))
    except Exception as exc:
        return Login(error=f"could not mint an admin token in the CMS: {exc}")


def _tls_probe(http: Http, web: PublicUrl) -> Probe:
    try:
        days = http.cert_days_left(web)
    except Exception as exc:
        return Probe("tls certificate", False, str(exc))
    return Probe("tls certificate", days is not None and days > 14, f"{days} days until expiry")


def _is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def deny_severity(ctx: Ctx, engine: PublicUrl) -> str:
    """Behind the bundled Traefik the deny is a rule the CLI rendered, so a hole is a failure.
    Behind an external proxy it is the operator's rule and may not be in place yet: a warning.
    With the engine URL and the bind address both on loopback there is no proxy and no
    network path either, so a permanent warning would only teach people to ignore warnings."""
    if ctx.scenario.proxy != "expose":
        return "fail"
    bind = ctx.env.get("NEOPS_BIND_ADDRESS", "127.0.0.1")
    if _is_loopback(engine.host) and _is_loopback(bind):
        return "skip"
    return "warn"


def run(
    ctx: Ctx,
    connect: str | None = None,
    insecure: bool = False,
    probe_ratelimit: bool = False,
    worker_grace: float = 0,
) -> list[Probe]:
    probes = container_probes(ctx.compose.ps())
    urls = public_urls(ctx.env, ctx.scenario)
    http = Http(connect=connect, insecure=insecure)
    probes += http_probes(urls, http, deny_severity(ctx, urls["NEOPS_ENGINE_URL"]))
    if ctx.scenario.logs:
        probes.append(logs_probe(urls["NEOPS_ENGINE_URL"], http))
    probes += _login_probes(ctx, urls["NEOPS_CMS_URL"], urls["NEOPS_ENGINE_URL"], http, worker_grace)
    if probe_ratelimit:
        probes.append(ratelimit_probe(urls["NEOPS_CMS_URL"], http, ctx.scenario.oidc))
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
