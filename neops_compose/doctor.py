from __future__ import annotations

import http.client
import json
import socket
import ssl
from dataclasses import dataclass

from neops_compose.compose import Compose
from neops_compose.env import Env
from neops_compose.scenario import Scenario
from neops_compose.urls import PublicUrl

ONE_SHOTS = {"cms-init"}
LOGIN_MUTATION = "mutation($u:String!,$p:String!){login(username:$u,password:$p){accessToken}}"


@dataclass(frozen=True)
class Probe:
    name: str
    ok: bool
    detail: str = ""


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

    def request(
        self,
        url: PublicUrl,
        path: str,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        conn = self._open(url)
        hdrs = {
            "Host": url.host if url.is_default_port else f"{url.host}:{url.port}",
            "User-Agent": "neops-doctor",
        }
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        try:
            conn.request(method, url.path + path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read().decode(errors="replace")
        finally:
            conn.close()

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


def _probe_get(
    out: list[Probe],
    http: Http,
    name: str,
    url: PublicUrl,
    path: str,
    want: int,
    contains: str = "",
    method: str = "GET",
    body: str | None = None,
) -> None:
    try:
        status, text = http.request(url, path, method=method, body=body)
    except Exception as exc:
        out.append(Probe(name, False, f"{url}{path}: {exc}"))
        return
    ok = status == want and (contains in text)
    detail = f"{url}{path} -> {status}"
    if not ok:
        detail += f", expected {want}" + (f" containing {contains!r}" if contains else "")
    out.append(Probe(name, ok, detail))


def _deny_probe(engine: PublicUrl, http: Http, expect_deny: bool) -> Probe:
    name = "engine worker API denied"
    try:
        status, _ = http.request(engine, "/blackboard/job", method="POST", body="{}")
    except Exception as exc:
        return Probe(name, False, f"{engine}/blackboard/job: {exc}")
    if status == 403:
        return Probe(name, True, f"{engine}/blackboard/job -> 403")
    return Probe(
        name,
        expect_deny is False,
        f"{engine}/blackboard/job -> {status}: the worker API is reachable from outside; "
        "your reverse proxy must deny it (see examples/external-proxy.env)",
    )


def http_probes(urls: dict[str, PublicUrl], http: Http, expect_deny: bool) -> list[Probe]:
    web, cms, engine, monitor = (
        urls[k] for k in ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL")
    )
    out: list[Probe] = []
    _probe_get(out, http, "web client", web, "/", 200, "app-root")
    _probe_get(out, http, "cms admin", cms, "/admin/login/", 200)
    _probe_get(
        out,
        http,
        "cms graphql",
        cms,
        "/graphql",
        200,
        "__typename",
        method="POST",
        body='{"query":"{__typename}"}',
    )
    _probe_get(out, http, "engine health", engine, "/health", 200)
    _probe_get(out, http, "monitor config", monitor, "/config.js", 200, str(engine))
    if "NEOPS_KEYCLOAK_URL" in urls:
        _probe_get(
            out,
            http,
            "keycloak realm",
            urls["NEOPS_KEYCLOAK_URL"],
            "/realms/neops/.well-known/openid-configuration",
            200,
            "authorization_endpoint",
        )
    if "NEOPS_GRAFANA_URL" in urls:
        _probe_get(out, http, "grafana", urls["NEOPS_GRAFANA_URL"], "/api/health", 200)
    out.append(_deny_probe(engine, http, expect_deny))
    return out


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
    return Probe("cms login path", True, f"bad credentials answered {status} (no server error)")


def login(cms: PublicUrl, http: Http, username: str, password: str) -> str | None:
    body = json.dumps({"query": LOGIN_MUTATION, "variables": {"u": username, "p": password}})
    status, text = http.request(cms, "/graphql", method="POST", body=body)
    try:
        return json.loads(text)["data"]["login"]["accessToken"]
    except Exception:
        return None


def worker_probe(engine: PublicUrl, http: Http, token: str | None) -> Probe:
    name = "worker registered"
    if not token:
        return Probe(name, False, "could not log in as the admin user to ask the engine")
    status, text = http.request(engine, "/workers", headers={"Authorization": f"Bearer {token}"})
    if status == 403:
        return Probe(
            name, True, "admin token accepted, no worker:read permission (assign a role to check further)"
        )
    if status != 200:
        return Probe(name, False, f"GET /workers -> {status}")
    try:
        workers = json.loads(text)
        items = workers if isinstance(workers, list) else workers.get("items", workers.get("data", []))
        online = [w for w in items if str(w.get("status", w.get("state", ""))).upper() == "ONLINE"]
        return Probe(name, bool(online), f"{len(online)} online of {len(items)} workers")
    except Exception as exc:
        return Probe(name, False, f"unexpected /workers payload: {exc}")


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


def _tls_probe(http: Http, web: PublicUrl) -> Probe:
    try:
        days = http.cert_days_left(web)
    except Exception as exc:
        return Probe("tls certificate", False, str(exc))
    return Probe("tls certificate", days is not None and days > 14, f"{days} days until expiry")


def run(
    env: Env,
    scenario: Scenario,
    compose: Compose,
    connect: str | None,
    insecure: bool,
    probe_ratelimit: bool = False,
) -> list[Probe]:
    probes = container_probes(compose.ps())
    urls = _public_urls(env, scenario)
    http = Http(connect=connect, insecure=insecure)
    probes += http_probes(urls, http, expect_deny=scenario.proxy == "traefik")
    probes.append(bad_login_probe(urls["NEOPS_CMS_URL"], http))
    if not scenario.oidc:
        token = login(
            urls["NEOPS_CMS_URL"],
            http,
            env.get("NEOPS_ADMIN_USER", "neops"),
            env.get("NEOPS_ADMIN_PASSWORD"),
        )
        probes.append(worker_probe(urls["NEOPS_ENGINE_URL"], http, token))
    if probe_ratelimit:
        probes.append(ratelimit_probe(urls["NEOPS_CMS_URL"], http))
    if scenario.tls:
        probes.append(_tls_probe(http, urls["NEOPS_WEB_URL"]))
    return probes


def format_report(probes: list[Probe]) -> str:
    return "\n".join(f"{'OK  ' if p.ok else 'FAIL'} {p.name:<28} {p.detail}".rstrip() for p in probes)


def all_ok(probes: list[Probe]) -> bool:
    return all(p.ok for p in probes)
