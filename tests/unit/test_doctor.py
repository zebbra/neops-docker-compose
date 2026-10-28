import contextlib
import json
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from neops_compose import doctor, secrets
from neops_compose.compose import ComposeError
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


UNKNOWN_COMMAND = "Unknown command: 'index_sync_status'\nType 'manage.py help' for usage.\n"


class IndexSyncCompose:
    """Answers `exec cms python manage.py index_sync_status --json` as a core image would."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0, fail: bool = False):
        self.result = (returncode, stdout, stderr)
        self.fail = fail
        self.runs = []

    def ps(self):
        return []

    def run(self, *args, capture=False, check=True, env=None, timeout=None):
        self.runs.append((args, timeout))
        if self.fail:
            raise ComposeError("docker is not installed or not on PATH")
        returncode, stdout, stderr = self.result
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


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


def test_the_monitor_config_probe_asks_under_the_monitor_path():
    """Under a path of the web client's origin, config.js lives under that prefix too."""
    shared = urls() | {
        "NEOPS_WEB_URL": PublicUrl.parse("https://neops.example.com"),
        "NEOPS_CMS_URL": PublicUrl.parse("https://neops.example.com"),
        "NEOPS_WORKFLOWS_URL": PublicUrl.parse("https://neops.example.com/workflows"),
    }
    http = healthy()
    probes = doctor.http_probes(shared, http)
    assert [p for p in probes if p.name == "monitor config"][0].ok
    assert ("https://neops.example.com/workflows/config.js", "GET", {}) in http.calls


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
    p = doctor.bad_login_probe(cms, http, oidc=False)
    assert not p.ok and "RATELIMIT" in p.detail
    http = FakeHttp({"/graphql": (200, '{"errors":[{"message":"Please enter valid credentials"}]}')})
    assert doctor.bad_login_probe(cms, http, oidc=False).ok


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

    probe = doctor.bad_login_probe(cms, FakeHttp({"/graphql": (200, down)}), oidc=False)
    assert not probe.ok and probe.detail == doctor.DB_UNREACHABLE

    admin = doctor.login(cms, FakeHttp({"/graphql": (200, down)}), "neops", "pw")
    assert admin.db_down and admin.token is None
    worker = doctor.worker_probe(engine, FakeHttp({}), admin)
    assert not worker.ok and worker.detail == doctor.DB_UNREACHABLE


def test_bad_credentials_are_not_mistaken_for_a_database_outage():
    cms = PublicUrl.parse("https://cms.neops.example.com")
    refused = '{"errors":[{"message":"Please enter valid credentials"}]}'
    assert doctor.bad_login_probe(cms, FakeHttp({"/graphql": (200, refused)}), oidc=False).ok
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
        compose=IndexSyncCompose(stderr=UNKNOWN_COMMAND, returncode=1),
        state=State(),
        log=lambda m: None,
    )


def _run_with(tmp_path, monkeypatch, compose_file: str, extra_env: str = "") -> list[doctor.Probe]:
    http = healthy({"/blackboard/job": (200, "{}")})
    monkeypatch.setattr(doctor, "Http", lambda **kwargs: http)
    ctx = _ctx(tmp_path, compose_file)
    if extra_env:
        (tmp_path / ".env").write_text((tmp_path / ".env").read_text() + extra_env)
        ctx = _ctx_from(tmp_path)
    return doctor.run(ctx)


def _ctx_from(tmp_path) -> Ctx:
    env = Env(tmp_path / ".env")
    return Ctx(
        repo=tmp_path,
        env=env,
        paths=Paths.for_repo(tmp_path, env),
        scenario=Scenario.from_env(env),
        compose=IndexSyncCompose(stderr=UNKNOWN_COMMAND, returncode=1),
        state=State(),
        log=lambda m: None,
    )


LOCAL_URLS = "".join(
    f"{k}={v}\n"
    for k, v in {
        "NEOPS_WEB_URL": "http://localhost:8080",
        "NEOPS_CMS_URL": "http://localhost:8000",
        "NEOPS_ENGINE_URL": "http://localhost:3030",
        "NEOPS_WORKFLOWS_URL": "http://localhost:3031",
    }.items()
)


def test_a_loopback_only_deployment_skips_the_deny_probe_instead_of_warning_forever(tmp_path, monkeypatch):
    """examples/local.env: no proxy will ever deny the worker routes, and with the engine on
    127.0.0.1 nothing off this host can reach them. A permanent WARN there is noise."""
    probes = _run_with(tmp_path, monkeypatch, "compose.yaml:compose.expose.yaml", LOCAL_URLS)
    probe = deny_probe_of(probes)
    assert probe.ok and probe.detail == doctor.LOOPBACK_ONLY


def test_loopback_urls_on_a_public_bind_address_still_warn(tmp_path, monkeypatch):
    extra = LOCAL_URLS + "NEOPS_BIND_ADDRESS=0.0.0.0\n"
    probes = _run_with(tmp_path, monkeypatch, "compose.yaml:compose.expose.yaml", extra)
    assert not deny_probe_of(probes).ok and deny_probe_of(probes).severity == "warn"


def test_run_warns_about_the_worker_api_in_expose_mode_and_fails_in_traefik_mode(tmp_path, monkeypatch):
    expose = _run_with(tmp_path, monkeypatch, "compose.yaml:compose.expose.yaml")
    assert deny_probe_of(expose).severity == "warn"

    traefik = _run_with(tmp_path, monkeypatch, "compose.yaml:compose.traefik.yaml")
    assert deny_probe_of(traefik).severity == "fail"
    assert doctor.all_ok(traefik) is False


OIDC = "compose.yaml:compose.traefik.yaml:compose.oidc.yaml"


def _oidc_ctx(tmp_path, compose) -> Ctx:
    ctx = _ctx(tmp_path, OIDC)
    ctx.compose = compose
    return ctx


class MintingCompose(IndexSyncCompose):
    def __init__(self, fail: bool = False):
        super().__init__(stderr=UNKNOWN_COMMAND, returncode=1)
        self.fail_exec = fail
        self.execs = []

    def exec(self, service, *cmd, env=None):
        self.execs.append((service, env))
        if self.fail_exec:
            raise ComposeError("no container")
        return "TOKEN=minted.jwt\n"


def test_under_oidc_the_worker_probe_asks_the_engine_with_a_token_minted_in_the_cms(tmp_path, monkeypatch):
    """Local login is off under OIDC, so the admin cannot log in; the CMS issues the token instead."""
    http = SequencedHttp(['[{"state":"online"}]'])
    monkeypatch.setattr(doctor, "Http", lambda **kwargs: http)
    compose = MintingCompose()

    probes = doctor.run(_oidc_ctx(tmp_path, compose))

    worker = [p for p in probes if p.name == doctor.WORKER_PROBE][0]
    assert worker.ok and worker.detail == "1 online of 1 workers"
    bearer = {"Authorization": "Bearer minted.jwt"}
    assert ("https://engine.neops.example.com/workers", "GET", bearer) in http.calls
    assert compose.execs == [("cms", {"NEOPS_USERNAME": "neops"})]


def test_under_oidc_a_token_that_cannot_be_minted_fails_the_worker_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "Http", lambda **kwargs: healthy())
    probes = doctor.run(_oidc_ctx(tmp_path, MintingCompose(fail=True)))
    worker = [p for p in probes if p.name == doctor.WORKER_PROBE][0]
    assert not worker.ok and "could not mint" in worker.detail


class CountingHttp(FakeHttp):
    def __init__(self, refuse_after: int | None):
        super().__init__({})
        self.refuse_after = refuse_after
        self.bodies = []

    def request(self, url, path, method="GET", body=None, headers=None):
        self.calls.append((str(url) + path, method, headers or {}))
        self.bodies.append(body)
        if self.refuse_after is not None and len(self.bodies) > self.refuse_after:
            return (200, '{"errors":[{"message":"Too many requests"}]}')
        return (200, '{"errors":[{"message":"refused"}]}')


def test_under_oidc_the_ratelimit_probe_spends_the_oidc_exchange_limit_not_the_disabled_login():
    """Core refuses `login` before its rate limiter when local login is off, so no number of
    forged logins could ever be refused: the probe would fail on every correct proxy."""
    cms = PublicUrl.parse("https://cms.neops.example.com")
    http = CountingHttp(refuse_after=20)
    probe = doctor.ratelimit_probe(cms, http, oidc=True)
    assert probe.ok
    assert len(http.bodies) == 21
    assert all("exchangeOidcCode" in b and "login(" not in b for b in http.bodies)


def test_under_oidc_the_ratelimit_probe_fails_when_no_exchange_is_refused():
    cms = PublicUrl.parse("https://cms.neops.example.com")
    probe = doctor.ratelimit_probe(cms, CountingHttp(refuse_after=None), oidc=True)
    assert not probe.ok and "21 forged" in probe.detail


class RecordingHttp(FakeHttp):
    def request(self, url, path, method="GET", body=None, headers=None):
        self.calls.append(body)
        return self.responses.get(path, (404, ""))


INVALID_CODE = '{"errors":[{"message":"Invalid authorization code"}],"data":{"exchangeOidcCode":null}}'


def test_under_oidc_the_cms_auth_path_probe_reaches_the_code_lookup_instead_of_the_disabled_login():
    """Core refuses `login` under OIDC before its rate limiter and its database, so a bad login
    proves neither; a well-formed unknown code passes both before it is refused."""
    cms = PublicUrl.parse("https://cms.neops.example.com")
    http = RecordingHttp({"/graphql": (200, INVALID_CODE)})
    probe = doctor.bad_login_probe(cms, http, oidc=True)
    assert probe.ok
    sent = json.loads(http.calls[0])
    assert "exchangeOidcCode" in sent["query"] and len(sent["variables"]["c"]) == 64


def test_under_oidc_anything_but_an_unknown_code_answer_fails_the_cms_auth_path_probe():
    cms = PublicUrl.parse("https://cms.neops.example.com")
    db_down = '{"errors":[{"message":"connection to server at \\"postgres-cms\\" failed"}]}'
    probe = doctor.bad_login_probe(cms, FakeHttp({"/graphql": (200, db_down)}), oidc=True)
    assert not probe.ok and "postgres-cms" in probe.detail
    meta_key = doctor.bad_login_probe(cms, FakeHttp({"/graphql": (500, "ImproperlyConfigured")}), oidc=True)
    assert not meta_key.ok and "RATELIMIT_IP_META_KEY" in meta_key.detail
    limited = '{"errors":[{"message":"Too many requests"}]}'
    probe = doctor.bad_login_probe(cms, FakeHttp({"/graphql": (200, limited)}), oidc=True)
    assert not probe.ok and "retry in a minute" in probe.detail


def test_without_oidc_the_ratelimit_probe_still_spends_six_logins():
    cms = PublicUrl.parse("https://cms.neops.example.com")
    http = CountingHttp(refuse_after=5)
    assert doctor.ratelimit_probe(cms, http, oidc=False).ok
    assert len(http.bodies) == 6 and all("login(" in b for b in http.bodies)
    assert not doctor.ratelimit_probe(cms, CountingHttp(refuse_after=None), oidc=False).ok


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


class SequencedHttp(FakeHttp):
    """Answers /workers with one payload per call, in order, repeating the last."""

    def __init__(self, workers_payloads: list[str]):
        super().__init__({})
        self.payloads = list(workers_payloads)

    def request(self, url, path, method="GET", body=None, headers=None):
        self.calls.append((str(url) + path, method, headers or {}))
        if path != "/workers":
            return (404, "")
        payload = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        return (200, payload)


def test_the_worker_probe_waits_for_a_worker_that_is_still_registering(monkeypatch):
    """Right after `up` the engine answers before the worker has registered its blocks: doctor
    asks again within the grace period instead of failing the start on a race."""
    engine = PublicUrl.parse("https://engine.neops.example.com")
    admin = doctor.Login(token="t")
    naps: list[float] = []
    monkeypatch.setattr(doctor.time, "sleep", naps.append)
    http = SequencedHttp(["[]", '[{"state":"offline"}]', '[{"state":"online"}]'])

    probe = doctor.settled_worker_probe(engine, http, admin, grace=60)

    assert probe.ok and probe.detail == "1 online of 1 workers"
    assert naps == [doctor.WORKER_POLL_SECONDS] * 2


def test_the_worker_probe_gives_up_after_the_grace_period(monkeypatch):
    engine = PublicUrl.parse("https://engine.neops.example.com")
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(doctor.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(doctor.time, "sleep", lambda s: None)

    probe = doctor.settled_worker_probe(engine, SequencedHttp(["[]"]), doctor.Login(token="t"), grace=25)

    assert not probe.ok and probe.pending and probe.detail == "0 online of 0 workers"


def test_a_plain_doctor_run_does_not_wait_for_workers(monkeypatch):
    """Only the start commands wait: a standalone `./neops doctor` answers now."""
    engine = PublicUrl.parse("https://engine.neops.example.com")
    monkeypatch.setattr(doctor.time, "sleep", lambda s: pytest.fail("slept"))
    probe = doctor.settled_worker_probe(engine, SequencedHttp(["[]"]), doctor.Login(token="t"), grace=0)
    assert not probe.ok


def index_sync_report(**overrides) -> str:
    report = {
        "version": 1,
        "models": {},
        "group_writes": {"count": 0, "awaiting_sweep": 0, "oldest_age_seconds": None},
        "unlocks": {
            "waiting": 0,
            "quarantined": 0,
            "awaiting_sweep": 0,
            "oldest_unquarantined_age_seconds": None,
        },
        "periodic_sweep_last_ran_at": "2026-10-27T18:00:00+00:00",
        "max_pending_age_seconds": 600,
        "grace_seconds": 60,
        "awaiting_sweep": 0,
        "permission_pending": 0,
        "problems": [],
    }
    return json.dumps(report | overrides)


def index_sync_probes(cms_tasks: bool = True, **answer) -> list[doctor.Probe]:
    return doctor.index_sync_probes(IndexSyncCompose(**answer), cms_tasks)


def test_index_sync_asks_core_in_the_cms_container_and_does_not_wait_forever():
    compose = IndexSyncCompose(stdout=index_sync_report())
    doctor.index_sync_probes(compose, cms_tasks=True)
    command = ("exec", "-T", "cms", "python", "manage.py", "index_sync_status", "--json")
    assert compose.runs == [(command, doctor.INDEX_SYNC_TIMEOUT_SECONDS)]


def test_a_core_image_without_the_status_command_skips_the_index_sync_probe():
    probes = index_sync_probes(stderr=UNKNOWN_COMMAND, returncode=1)
    assert [(p.ok, p.detail) for p in probes] == [(True, doctor.INDEX_SYNC_UNAVAILABLE)]


def test_an_index_in_sync_reports_what_waits_and_when_the_sweep_last_ran():
    (probe,) = index_sync_probes(stdout="neops.core ready\n" + index_sync_report(permission_pending=2) + "\n")
    assert probe.ok
    assert probe.detail == (
        "0 index writes due for the sweep, 2 rows hidden from restricted users; "
        "periodic sweep last ran: 2026-10-27T18:00:00+00:00"
    )


def test_each_problem_core_reports_is_a_warning_that_does_not_fail_doctor():
    problems = ["core.device: 1 rows in error", "core.client: 2 rows rejected by Elasticsearch"]
    probes = index_sync_probes(stdout=index_sync_report(problems=problems, awaiting_sweep=1), returncode=1)
    assert [(p.ok, p.severity, p.detail) for p in probes] == [(False, "warn", text) for text in problems]
    assert doctor.all_ok(probes)


def test_index_writes_due_for_the_sweep_without_cms_tasks_are_a_warning():
    probes = index_sync_probes(cms_tasks=False, stdout=index_sync_report(awaiting_sweep=3))
    assert [p.detail for p in probes] == [doctor.CMS_TASKS_OFF.format(waiting=3, commands=doctor.RESYNC_ROWS)]
    assert probes[0].severity == "warn"


def _due_unlocks(count: int) -> dict:
    return {
        "waiting": count,
        "quarantined": 0,
        "awaiting_sweep": count,
        "oldest_unquarantined_age_seconds": 1.0,
    }


def test_without_cms_tasks_the_warning_names_the_command_for_what_is_due():
    rows_only = index_sync_probes(cms_tasks=False, stdout=index_sync_report(awaiting_sweep=2))
    unlocks_only = index_sync_probes(
        cms_tasks=False, stdout=index_sync_report(awaiting_sweep=1, unlocks=_due_unlocks(1))
    )
    both = index_sync_probes(
        cms_tasks=False, stdout=index_sync_report(awaiting_sweep=3, unlocks=_due_unlocks(1))
    )

    assert doctor.RESYNC_ROWS in rows_only[0].detail and doctor.RESYNC_UNLOCKS not in rows_only[0].detail
    assert (
        doctor.RESYNC_UNLOCKS in unlocks_only[0].detail and doctor.RESYNC_ROWS not in unlocks_only[0].detail
    )
    assert doctor.RESYNC_ROWS in both[0].detail and doctor.RESYNC_UNLOCKS in both[0].detail


def test_with_cms_tasks_on_index_writes_due_for_the_sweep_are_not_a_warning():
    (probe,) = index_sync_probes(cms_tasks=True, stdout=index_sync_report(awaiting_sweep=3))
    assert probe.ok


def test_without_cms_tasks_writes_still_on_their_way_are_fine():
    """Core counts only what the sweep would take now, not rows whose own write is in flight."""
    (probe,) = index_sync_probes(cms_tasks=False, stdout=index_sync_report())
    assert probe.ok


def test_a_cms_that_does_not_answer_with_a_report_is_a_one_line_warning():
    traceback = "Traceback (most recent call last):\n  File x\npsycopg.OperationalError: connection refused\n"
    probes = index_sync_probes(stderr=traceback, returncode=1)
    assert [(p.ok, p.severity) for p in probes] == [(False, "warn")]
    assert "\n" not in probes[0].detail
    assert "connection refused" in probes[0].detail


def test_a_report_without_the_keys_doctor_reads_is_not_mistaken_for_a_healthy_one():
    for answer in ('{"models": {}}', index_sync_report(version=2), '{"version": 1, "problems": "none"}'):
        probes = index_sync_probes(stdout=answer)
        assert [(p.ok, p.severity) for p in probes] == [(False, "warn")], answer


def test_a_compose_failure_or_timeout_is_a_warning():
    probes = index_sync_probes(fail=True)
    assert [(p.ok, p.severity) for p in probes] == [(False, "warn")]
    assert "could not ask the CMS" in probes[0].detail


def test_run_reports_the_index_sync(tmp_path, monkeypatch):
    probes = _run_with(tmp_path, monkeypatch, "compose.yaml:compose.traefik.yaml")
    assert [p.detail for p in probes if p.name == doctor.INDEX_SYNC_PROBE] == [doctor.INDEX_SYNC_UNAVAILABLE]
