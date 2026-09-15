from neops_compose import doctor
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


def test_http_probes_hosts_mode():
    http = FakeHttp(
        {
            "/": (200, "<html><app-root></app-root>"),
            "/admin/login/": (200, "login"),
            "/graphql": (200, '{"data":{"__typename":"Query"}}'),
            "/health": (200, '{"healthy":true}'),
            "/blackboard/job": (403, "Forbidden"),
            "/config.js": (200, 'apiBaseUrl: "https://engine.neops.example.com"'),
        }
    )
    probes = doctor.http_probes(urls(), http)
    assert all(p.ok for p in probes), [p for p in probes if not p.ok]
    assert ("https://engine.neops.example.com/blackboard/job", "POST", {}) in http.calls


def test_deny_probe_fails_whenever_the_worker_api_answers_anything_but_403():
    for status in (200, 201, 400, 401, 404, 500):
        http = FakeHttp({"/blackboard/job": (status, "whatever")})
        deny = [p for p in doctor.http_probes(urls(), http) if p.name == "engine worker API denied"][0]
        assert not deny.ok and "reachable" in deny.detail, status


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


def test_worker_probe_names_the_rate_limit():
    engine = PublicUrl.parse("https://engine.neops.example.com")
    p = doctor.worker_probe(engine, FakeHttp({}), doctor.Login(rate_limited=True, error="429: Too many"))
    assert not p.ok and "rate limit" in p.detail and "retry in a minute" in p.detail
