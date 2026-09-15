import contextlib
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from neops_compose import doctor, secrets
from neops_compose.context import Ctx
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.state import State
from neops_compose.urls import PublicUrl

HOSTS = {
    "NEOPS_WEB_URL": "https://neops.example.com",
    "NEOPS_CMS_URL": "https://cms.neops.example.com",
    "NEOPS_ENGINE_URL": "https://engine.neops.example.com",
    "NEOPS_WORKFLOWS_URL": "https://workflows.neops.example.com",
}


def urls() -> dict[str, PublicUrl]:
    return {k: PublicUrl.parse(v) for k, v in HOSTS.items()}


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, url, path, method="GET", body=None, headers=None):
        self.calls.append((str(url) + path, method, headers or {}))
        return self.responses.get(path, (404, ""))


def test_container_probes_treat_completed_init_as_ok():
    rows = [
        {"Service": "cms-init", "State": "exited", "ExitCode": 0, "Health": ""},
        {"Service": "cms", "State": "running", "Health": "healthy"},
        {"Service": "worker", "State": "running", "Health": ""},
        {"Service": "engine", "State": "running", "Health": "unhealthy"},
    ]
    probes = doctor.container_probes(rows, one_shots={"cms-init"})
    by = {p.name: p for p in probes}
    assert by["cms-init"].ok and by["cms"].ok and by["worker"].ok and not by["engine"].ok


HEALTHY = {
    "/": (200, "<html><app-root></app-root>"),
    "/admin/login/": (200, "login"),
    "/graphql": (200, '{"data":{"__typename":"Query"}}'),
    "/health": (200, '{"healthy":true}'),
    "/blackboard/job": (403, "Forbidden"),
    "/config.js": (200, 'apiBaseUrl: "https://engine.neops.example.com"'),
}


def healthy(overrides: dict | None = None) -> FakeHttp:
    return FakeHttp(HEALTHY | (overrides or {}))


def test_http_probes_hosts_mode():
    http = healthy()
    probes = doctor.http_probes(urls(), http)
    assert all(p.ok for p in probes), [p for p in probes if not p.ok]
    assert ("https://engine.neops.example.com/blackboard/job", "POST", {}) in http.calls


def deny_probe_of(probes: list[doctor.Probe]) -> doctor.Probe:
    return [p for p in probes if p.name == "engine worker API denied"][0]


def test_deny_probe_fails_whenever_the_worker_api_answers_anything_but_403():
    for status in (200, 201, 400, 401, 404, 500):
        http = FakeHttp({"/blackboard/job": (status, "whatever")})
        deny = deny_probe_of(doctor.http_probes(urls(), http))
        assert not deny.ok and "reachable" in deny.detail, status
        assert deny.severity == "fail"


def test_in_traefik_mode_an_undenied_worker_api_is_a_hard_failure():
    """Traefik carries the deny middleware itself, so nothing outside this deployment can
    explain the route answering: a proxy the CLI configured is not doing what it was told."""
    probes = doctor.http_probes(urls(), healthy({"/blackboard/job": (200, "{}")}))
    assert doctor.all_ok(probes) is False
    assert "FAIL engine worker API denied" in doctor.format_report(probes)


def test_in_expose_mode_an_undenied_worker_api_is_a_warning_doctor_still_passes():
    """No proxy is in front on a first install, so the deny cannot hold yet. Blocking there
    would mean `install` can never finish in expose mode."""
    probes = doctor.http_probes(urls(), healthy({"/blackboard/job": (200, "{}")}), "warn")
    deny = deny_probe_of(probes)
    assert not deny.ok and deny.severity == "warn"
    assert doctor.EXTERNAL_PROXY_DENY_HINT in deny.detail
    assert doctor.all_ok(probes) is True
    assert "WARN engine worker API denied" in doctor.format_report(probes)


def test_a_warning_severity_never_downgrades_another_failing_probe():
    probes = [
        doctor.Probe("engine worker API denied", False, "", "warn"),
        doctor.Probe("cms graphql", False, "connection refused"),
    ]
    assert doctor.all_ok(probes) is False


def test_an_expose_mode_proxy_that_does_deny_still_reports_ok():
    probes = doctor.http_probes(urls(), healthy(), "warn")
    deny = deny_probe_of(probes)
    assert deny.ok and "403" in deny.detail
    assert "OK   engine worker API denied" in doctor.format_report(probes)


def test_an_unreachable_engine_keeps_the_severity_of_its_mode():
    class Refusing:
        def request(self, *a, **k):
            raise OSError("connection refused")

    assert deny_probe_of(doctor.http_probes(urls(), Refusing(), "warn")).severity == "warn"
    assert deny_probe_of(doctor.http_probes(urls(), Refusing())).severity == "fail"


def test_login_probe_never_accepts_500():
    cms = PublicUrl.parse("https://cms.neops.example.com")
    http = FakeHttp({"/graphql": (500, "ImproperlyConfigured RATELIMIT_IP_META_KEY")})
    p = doctor.bad_login_probe(cms, http)
    assert not p.ok and "RATELIMIT" in p.detail
    http = FakeHttp({"/graphql": (200, '{"errors":[{"message":"Please enter valid credentials"}]}')})
    assert doctor.bad_login_probe(cms, http).ok


def test_login_separates_bad_credentials_from_the_rate_limit():
    cms = PublicUrl.parse("https://cms.neops.example.com")
    bad = doctor.login(cms, FakeHttp({"/graphql": (200, '{"errors":[{"message":"invalid"}]}')}), "n", "p")
    assert bad.token is None and bad.rate_limited is False
    limited = doctor.login(cms, FakeHttp({"/graphql": (429, "Too many requests")}), "n", "p")
    assert limited.token is None and limited.rate_limited is True
    good = FakeHttp({"/graphql": (200, '{"data":{"login":{"accessToken":"t"}}}')})
    assert doctor.login(cms, good, "n", "p").token == "t"


def test_a_cms_that_cannot_reach_its_database_fails_both_login_probes():
    """Core answers `internal error` while Postgres is gone, as a 200 carrying a GraphQL error.
    Without this it reads as an ordinary refused login and doctor goes green on a deployment
    that cannot serve a single request."""
    cms = PublicUrl.parse("https://cms.neops.example.com")
    engine = PublicUrl.parse("https://engine.neops.example.com")
    down = '{"errors":[{"message":"internal error"}],"data":{"login":null}}'

    probe = doctor.bad_login_probe(cms, FakeHttp({"/graphql": (200, down)}))
    assert not probe.ok and probe.detail == doctor.DB_UNREACHABLE

    admin = doctor.login(cms, FakeHttp({"/graphql": (200, down)}), "neops", "pw")
    assert admin.db_down and admin.token is None
    worker = doctor.worker_probe(engine, FakeHttp({}), admin)
    assert not worker.ok and worker.detail == doctor.DB_UNREACHABLE


def test_bad_credentials_are_not_mistaken_for_a_database_outage():
    cms = PublicUrl.parse("https://cms.neops.example.com")
    refused = '{"errors":[{"message":"Please enter valid credentials"}]}'
    assert doctor.bad_login_probe(cms, FakeHttp({"/graphql": (200, refused)})).ok
    assert not doctor.login(cms, FakeHttp({"/graphql": (200, refused)}), "n", "p").db_down


def _ctx(tmp_path, compose_file: str) -> Ctx:
    lines = [f"COMPOSE_FILE={compose_file}\n"] + [f"{k}={v}\n" for k, v in HOSTS.items()]
    (tmp_path / ".env").write_text("".join(lines))
    env = Env(tmp_path / ".env")
    return Ctx(
        repo=tmp_path,
        env=env,
        paths=Paths.for_repo(tmp_path, env),
        scenario=Scenario.from_env(env),
        compose=type("C", (), {"ps": staticmethod(lambda: [])})(),
        state=State(),
        log=lambda m: None,
    )


def _run_with(tmp_path, monkeypatch, compose_file: str) -> list[doctor.Probe]:
    http = healthy({"/blackboard/job": (200, "{}")})
    monkeypatch.setattr(doctor, "Http", lambda **kwargs: http)
    return doctor.run(_ctx(tmp_path, compose_file))


def test_run_warns_about_the_worker_api_in_expose_mode_and_fails_in_traefik_mode(tmp_path, monkeypatch):
    expose = _run_with(tmp_path, monkeypatch, "compose.yaml:compose.expose.yaml")
    assert deny_probe_of(expose).severity == "warn"

    traefik = _run_with(tmp_path, monkeypatch, "compose.yaml:compose.traefik.yaml")
    assert deny_probe_of(traefik).severity == "fail"
    assert doctor.all_ok(traefik) is False


def test_worker_probe_names_the_rate_limit():
    engine = PublicUrl.parse("https://engine.neops.example.com")
    p = doctor.worker_probe(engine, FakeHttp({}), doctor.Login(rate_limited=True, error="429: Too many"))
    assert not p.ok and "rate limit" in p.detail and "retry in a minute" in p.detail


class EchoHandler(BaseHTTPRequestHandler):
    """Answers with the Host header it received, which is what the connect override changes."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = json.dumps({"host": self.headers["Host"], "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Neops-Probe", "answered")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class QuietServer(ThreadingHTTPServer):
    """cert_days_left opens a connection and reads the certificate without sending a request,
    which the default server reports on stderr as a broken client."""

    def handle_error(self, request, client_address) -> None:
        pass


@contextlib.contextmanager
def serving(tls_dir=None):
    try:
        httpd = QuietServer(("127.0.0.1", 0), EchoHandler)
    except OSError as exc:  # a sandbox that forbids listening sockets
        pytest.skip(f"cannot bind a local socket here: {exc}")
    if tls_dir is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tls_dir / "cert.pem", tls_dir / "key.pem")
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def selfsigned(tmp_path, host: str):
    tls_dir = tmp_path / "tls"
    secrets.ensure_selfsigned(tls_dir, [host])
    return tls_dir


def test_http_connects_to_one_address_while_sending_another_host():
    """Boxes without DNS for the public hostnames resolve nothing, so doctor dials --connect
    and still has to present the public name, or the proxy routes the request nowhere."""
    with serving() as port:
        url = PublicUrl.parse(f"http://neops.example.com:{port}")
        status, headers, text = doctor.Http(connect="127.0.0.1").fetch(url, "/health")
    assert status == 200
    assert headers["X-Neops-Probe"] == "answered"
    assert json.loads(text) == {"host": f"neops.example.com:{port}", "path": "/health"}


def test_https_reaches_a_self_signed_server_and_reads_its_certificate(tmp_path):
    tls_dir = selfsigned(tmp_path, "neops.example.com")
    with serving(tls_dir) as port:
        url = PublicUrl.parse(f"https://neops.example.com:{port}")
        http = doctor.Http(connect="127.0.0.1", insecure=True)
        status, _, text = http.fetch(url, "/")
        days = http.cert_days_left(url)
    assert status == 200 and json.loads(text)["host"] == f"neops.example.com:{port}"
    assert 395 <= days <= 397, days


def test_a_certificate_close_to_expiry_fails_the_tls_probe(tmp_path):
    tls_dir = tmp_path / "tls"
    secrets.ensure_selfsigned(tls_dir, ["neops.example.com"], days=10)
    with serving(tls_dir) as port:
        url = PublicUrl.parse(f"https://neops.example.com:{port}")
        http = doctor.Http(connect="127.0.0.1", insecure=True)
        probe = doctor._tls_probe(http, url)
        assert not probe.ok and "9 days" in probe.detail
        secrets.ensure_selfsigned(tls_dir, ["neops.example.com"], rotate=True)
    assert doctor._tls_probe(http, PublicUrl.parse("http://neops.example.com")).ok is False


def test_cert_days_left_is_none_over_plain_http():
    with serving() as port:
        assert doctor.Http(connect="127.0.0.1").cert_days_left(PublicUrl.parse(f"http://x:{port}")) is None
