from neops_compose import doctor
from neops_compose.urls import PublicUrl


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
    urls = {
        k: PublicUrl.parse(v)
        for k, v in {
            "NEOPS_WEB_URL": "https://neops.example.com",
            "NEOPS_CMS_URL": "https://cms.neops.example.com",
            "NEOPS_ENGINE_URL": "https://engine.neops.example.com",
            "NEOPS_WORKFLOWS_URL": "https://workflows.neops.example.com",
        }.items()
    }
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
    probes = doctor.http_probes(urls, http, expect_deny=True)
    assert all(p.ok for p in probes), [p for p in probes if not p.ok]
    assert ("https://engine.neops.example.com/blackboard/job", "POST", {}) in http.calls


def test_deny_probe_fails_when_worker_api_is_reachable():
    urls = {
        "NEOPS_WEB_URL": PublicUrl.parse("https://neops.example.com"),
        "NEOPS_CMS_URL": PublicUrl.parse("https://cms.neops.example.com"),
        "NEOPS_ENGINE_URL": PublicUrl.parse("https://engine.neops.example.com"),
        "NEOPS_WORKFLOWS_URL": PublicUrl.parse("https://workflows.neops.example.com"),
    }
    http = FakeHttp({"/blackboard/job": (400, "validation error")})
    probes = doctor.http_probes(urls, http, expect_deny=True)
    deny = [p for p in probes if p.name == "engine worker API denied"][0]
    assert not deny.ok and "reachable" in deny.detail


def test_login_probe_never_accepts_500():
    cms = PublicUrl.parse("https://cms.neops.example.com")
    http = FakeHttp({"/graphql": (500, "ImproperlyConfigured RATELIMIT_IP_META_KEY")})
    p = doctor.bad_login_probe(cms, http)
    assert not p.ok and "RATELIMIT" in p.detail
    http = FakeHttp({"/graphql": (200, '{"errors":[{"message":"Please enter valid credentials"}]}')})
    assert doctor.bad_login_probe(cms, http).ok
