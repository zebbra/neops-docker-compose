# tests/unit/test_traefik_model.py
import re

from neops_compose.env import Env
from neops_compose.scenario import Scenario
from neops_compose.traefik_model import CMS_SLASH_MIDDLEWARE, build_traefik, static_config, worker_deny_rule

HOSTS = """
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_KEYCLOAK_URL=https://auth.neops.example.com
"""
SHARED = """
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.traefik-shared-host.yaml:compose.tls-acme.yaml:compose.oidc.yaml:compose.keycloak.yaml
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://neops.example.com
NEOPS_ENGINE_URL=https://neops.example.com/engine
NEOPS_WORKFLOWS_URL=https://neops.example.com:8443
NEOPS_KEYCLOAK_URL=https://neops.example.com/sso
NEOPS_ACME_EMAIL=ops@example.com
"""


def cfg(tmp_path, text):
    (tmp_path / ".env").write_text(text)
    env = Env(tmp_path / ".env")
    return build_traefik(env, Scenario.from_env(env))


def by_name(cfg):
    return {r.name: r for r in cfg.routers}


def test_hosts_mode_routers_and_tls_files(tmp_path):
    c = cfg(tmp_path, HOSTS)
    r = by_name(c)
    assert (
        r["web"].rule == "Host(`neops.example.com`)" and r["web"].entrypoint == "websecure" and r["web"].tls
    )
    assert r["cms"].rule == "Host(`cms.neops.example.com`)" and r["cms"].service == "cms"
    assert r["engine"].middlewares == () and r["engine"].service == "engine"
    assert "keycloak" not in r  # no keycloak overlay in HOSTS
    assert r["engine-deny-worker-api"].priority > r["engine"].priority
    assert r["engine-deny-worker-api"].rule.startswith(
        "Host(`engine.neops.example.com`) && Method(`POST`) && PathRegexp(`^(?i:/("
    )
    assert "blackboard/job(/.*)?" in r["engine-deny-worker-api"].rule
    assert r["engine-deny-worker-api"].middlewares == ("deny-worker-api",)
    assert c.middlewares["deny-worker-api"] == {"ipAllowList": {"sourceRange": ["192.0.2.1/32"]}}
    assert [e.name for e in c.entrypoints] == ["web", "websecure"]
    assert c.entrypoints[0].redirect_to == "websecure"
    assert c.tls_mode == "files" and c.services["monitor"] == "http://monitor:80"


def test_shared_host_mode(tmp_path):
    c = cfg(tmp_path, SHARED)
    r = by_name(c)
    cms_routers = [x for x in c.routers if x.service == "cms"]
    assert len(cms_routers) == 10
    assert all(x.rule.startswith("Host(`neops.example.com`) && PathPrefix(`/") for x in cms_routers)
    assert all(x.priority > r["web"].priority for x in cms_routers)
    assert r["engine"].rule == "Host(`neops.example.com`) && PathPrefix(`/engine`)"
    assert r["engine"].middlewares == ("engine-strip",)
    assert c.middlewares["engine-strip"] == {"stripPrefix": {"prefixes": ["/engine"]}}
    assert r["engine-deny-worker-api"].rule.startswith(
        "Host(`neops.example.com`) && Method(`POST`) && PathRegexp(`^/engine(?i:/("
    )
    assert "workers/[^/]+/(ping|unregister)" in r["engine-deny-worker-api"].rule
    assert r["monitor"].entrypoint == "monitor" and r["monitor"].rule == "Host(`neops.example.com`)"
    assert r["keycloak"].middlewares == () and r["keycloak"].rule.endswith("PathPrefix(`/sso`)")
    assert [e.name for e in c.entrypoints] == ["web", "websecure", "monitor"]
    assert c.tls_mode == "acme" and c.acme_email == "ops@example.com"


def test_http_only_uses_web_entrypoint_without_tls(tmp_path):
    text = HOSTS.replace(":compose.tls-files.yaml", "").replace("https://", "http://")
    c = cfg(tmp_path, text)
    assert all(x.entrypoint == "web" and not x.tls for x in c.routers)
    assert [e.name for e in c.entrypoints] == ["web"] and c.entrypoints[0].redirect_to is None


def test_model_is_deterministic(tmp_path):
    assert cfg(tmp_path, SHARED) == cfg(tmp_path, SHARED)


def _pattern(rule: str) -> str:
    return rule.split("PathRegexp(`", 1)[1].rsplit("`)", 1)[0]


def test_worker_deny_rule_is_case_insensitive_and_slash_tolerant():
    rule = worker_deny_rule("engine.example.com", "")
    pattern = re.compile(_pattern(rule))
    for path in ("/workers/register/", "/Workers/Register", "/blackboard/job/result"):
        assert pattern.match(path), path
    for path in ("/workers/cleanup", "/workers", "/blackboard/jobs", "/function-blocks"):
        assert not pattern.match(path), path


def test_worker_deny_rule_honours_the_engine_path_prefix():
    rule = worker_deny_rule("neops.example.com", "/engine")
    pattern = re.compile(_pattern(rule))
    assert pattern.match("/engine/workers/abc/ping/")
    assert not pattern.match("/workers/register")


SHARED_PATHS = SHARED.replace("https://neops.example.com:8443", "https://neops.example.com/workflows")


def test_shared_host_monitor_under_a_path_rides_the_public_entrypoint(tmp_path):
    """The path form has no monitor entrypoint at all: the monitor is routed like the engine,
    prefix stripped, above the web client's catch-all."""
    c = cfg(tmp_path, SHARED_PATHS)
    r = by_name(c)
    assert r["monitor"].rule == "Host(`neops.example.com`) && PathPrefix(`/workflows`)"
    assert r["monitor"].entrypoint == "websecure" and r["monitor"].tls
    assert r["monitor"].priority > r["web"].priority
    assert r["monitor"].middlewares == ("monitor-strip",)
    assert c.middlewares["monitor-strip"] == {"stripPrefix": {"prefixes": ["/workflows"]}}
    assert [e.name for e in c.entrypoints] == ["web", "websecure"]
    assert c.services["monitor"] == "http://monitor:80"


def test_shared_host_monitor_on_its_own_hostname_has_no_monitor_entrypoint(tmp_path):
    c = cfg(tmp_path, SHARED.replace("https://neops.example.com:8443", "https://workflows.neops.example.com"))
    r = by_name(c)
    assert (
        r["monitor"].rule == "Host(`workflows.neops.example.com`)" and r["monitor"].entrypoint == "websecure"
    )
    assert r["monitor"].middlewares == ()
    assert [e.name for e in c.entrypoints] == ["web", "websecure"]


SHARED_NO_TLS = """
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.traefik-shared-host.yaml
NEOPS_WEB_URL=http://neops.example.com
NEOPS_CMS_URL=http://neops.example.com
NEOPS_ENGINE_URL=http://neops.example.com/engine
NEOPS_WORKFLOWS_URL=http://neops.example.com:8443
"""


def test_shared_host_without_tls_has_no_tls_routers(tmp_path):
    c = cfg(tmp_path, SHARED_NO_TLS)
    assert all(not x.tls for x in c.routers)
    r = by_name(c)
    assert r["monitor"].entrypoint == "monitor"


def test_https_redirect_targets_the_websecure_entrypoint_at_the_default_port(tmp_path):
    c = cfg(tmp_path, HOSTS)
    static = static_config(c)
    entry = static["entryPoints"]["web"]["http"]["redirections"]["entryPoint"]
    assert entry["to"] == "websecure" and "port" not in entry


def test_https_redirect_targets_a_non_default_port_directly(tmp_path):
    c = cfg(tmp_path, HOSTS + "NEOPS_HTTPS_PORT=8443\n")
    static = static_config(c)
    entry = static["entryPoints"]["web"]["http"]["redirections"]["entryPoint"]
    assert entry["to"] == ":8443" and "port" not in entry


def test_every_cms_router_redirects_the_bare_directory_prefixes(tmp_path):
    """Core answers `/admin` (no slash) with a 500 (neops-core #2276), so the proxy issues the
    slash redirect Django would have; the regex sees the whole URL and keeps scheme and host."""
    for text in (HOSTS, SHARED):
        c = cfg(tmp_path, text)
        cms_routers = [r for r in c.routers if r.service == "cms"]
        assert cms_routers and all(CMS_SLASH_MIDDLEWARE in r.middlewares for r in cms_routers)
        assert all(CMS_SLASH_MIDDLEWARE not in r.middlewares for r in c.routers if r.service != "cms")
    redirect = c.middlewares[CMS_SLASH_MIDDLEWARE]["redirectRegex"]
    assert redirect["replacement"] == "${1}/" and redirect["permanent"] is True
    pattern = re.compile(redirect["regex"])
    for url in ("https://neops.example.com/admin", "http://cms.neops.example.com:8080/accounts"):
        assert pattern.sub(r"\1/", url) == url + "/", url
    for url in (
        "https://neops.example.com/admin/",
        "https://neops.example.com/admin/login/",
        "https://neops.example.com/administrator",
        "https://neops.example.com/graphql",
    ):
        assert pattern.search(url) is None, url
