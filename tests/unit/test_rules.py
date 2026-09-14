from pathlib import Path

import pytest

from neops_compose.env import Env
from neops_compose.rules import problems
from neops_compose.scenario import Scenario

GOOD = """
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml
COMPOSE_PATH_SEPARATOR=:
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_CMS_DB_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f6
NEOPS_ENGINE_DB_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f7
DJANGO_SECRET_KEY=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6
NEOPS_ADMIN_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f8
NEOPS_TLS_CERT_FILE=./certs/cert.pem
NEOPS_TLS_KEY_FILE=./certs/key.pem
"""


def make(tmp_repo: Path, text: str) -> tuple[Env, Scenario]:
    (tmp_repo / ".env").write_text(text)
    env = Env(tmp_repo / ".env")
    return env, Scenario.from_env(env)


def test_good_env_has_no_problems(tmp_repo):
    env, sc = make(tmp_repo, GOOD)
    assert problems(env, sc, tmp_repo) == []


def test_missing_required_key_and_placeholder_secret(tmp_repo):
    text = GOOD.replace(
        "NEOPS_ADMIN_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f8", "NEOPS_ADMIN_PASSWORD=changeme"
    ).replace("NEOPS_CMS_DB_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f6\n", "")
    env, sc = make(tmp_repo, text)
    out = problems(env, sc, tmp_repo)
    assert any("NEOPS_CMS_DB_PASSWORD" in p for p in out)
    assert any("NEOPS_ADMIN_PASSWORD" in p and "placeholder" in p for p in out)


def test_cms_url_with_path_is_rejected(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace("https://cms.neops.example.com", "https://neops.example.com/cms"))
    assert any("NEOPS_CMS_URL" in p and "path" in p for p in problems(env, sc, tmp_repo))


def test_cms_on_web_origin_requires_shared_host_overlay(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace("https://cms.neops.example.com", "https://neops.example.com"))
    assert any("compose.traefik-shared-host.yaml" in p for p in problems(env, sc, tmp_repo))


def test_shared_host_accepts_engine_path_and_distinct_monitor_origin(tmp_repo):
    text = (
        GOOD.replace("compose.traefik.yaml:", "compose.traefik.yaml:compose.traefik-shared-host.yaml:")
        .replace("https://cms.neops.example.com", "https://neops.example.com")
        .replace("https://engine.neops.example.com", "https://neops.example.com/engine")
        .replace("https://workflows.neops.example.com", "https://neops.example.com:8443")
    )
    env, sc = make(tmp_repo, text)
    assert problems(env, sc, tmp_repo) == []


def test_monitor_must_not_share_web_origin(tmp_repo):
    env, sc = make(
        tmp_repo, GOOD.replace("https://workflows.neops.example.com", "https://neops.example.com/workflows")
    )
    assert any("NEOPS_WORKFLOWS_URL" in p and "origin" in p for p in problems(env, sc, tmp_repo))


def test_engine_path_must_not_collide_with_core_or_web_paths(tmp_repo):
    env, sc = make(
        tmp_repo, GOOD.replace("https://engine.neops.example.com", "https://neops.example.com/auth")
    )
    assert any("NEOPS_ENGINE_URL" in p and "reserved" in p for p in problems(env, sc, tmp_repo))


@pytest.mark.parametrize(
    "compose_file,needle",
    [
        ("compose.yaml", "compose.expose.yaml or compose.traefik.yaml"),
        ("compose.yaml:compose.expose.yaml:compose.traefik.yaml", "not both"),
        ("compose.yaml:compose.expose.yaml:compose.tls-files.yaml", "compose.traefik.yaml"),
        ("compose.yaml:compose.traefik.yaml:compose.tls-files.yaml:compose.tls-acme.yaml", "one of"),
        ("compose.yaml:compose.traefik.yaml:compose.keycloak.yaml", "compose.oidc.yaml"),
        ("compose.yaml:compose.traefik.yaml:compose.nope.yaml", "does not exist"),
    ],
)
def test_overlay_combination_rules(tmp_repo, compose_file, needle):
    text = GOOD.replace(
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml",
        f"COMPOSE_FILE={compose_file}",
    )
    env, sc = make(tmp_repo, text)
    assert any(needle in p for p in problems(env, sc, tmp_repo)), problems(env, sc, tmp_repo)


def test_acme_requires_port_80(tmp_repo):
    text = (
        GOOD.replace("compose.tls-files.yaml", "compose.tls-acme.yaml")
        + "NEOPS_ACME_EMAIL=ops@example.com\nNEOPS_HTTP_PORT=8880\n"
    )
    env, sc = make(tmp_repo, text)
    assert any("NEOPS_HTTP_PORT" in p for p in problems(env, sc, tmp_repo))


def test_oidc_external_requires_provider_keys_but_keycloak_does_not(tmp_repo):
    base = GOOD.replace("compose.tls-files.yaml", "compose.tls-files.yaml:compose.oidc.yaml")
    env, sc = make(tmp_repo, base)
    assert any("NEOPS_OIDC_CLIENT_ID" in p for p in problems(env, sc, tmp_repo))
    kc = (
        base.replace("compose.oidc.yaml", "compose.oidc.yaml:compose.keycloak.yaml")
        + "NEOPS_KEYCLOAK_URL=https://auth.neops.example.com\nNEOPS_KEYCLOAK_ADMIN_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f9\nNEOPS_KEYCLOAK_DB_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5fa\n"
    )
    env, sc = make(tmp_repo, kc)
    assert problems(env, sc, tmp_repo) == []


def test_oidc_missing_client_secret_reports_once(tmp_repo):
    text = GOOD.replace("compose.tls-files.yaml", "compose.tls-files.yaml:compose.oidc.yaml")
    env, sc = make(tmp_repo, text)
    out = problems(env, sc, tmp_repo)
    assert sum("NEOPS_OIDC_CLIENT_SECRET" in p for p in out) == 1


def test_cms_url_with_path_yields_exactly_one_message(tmp_repo):
    text = GOOD.replace("https://cms.neops.example.com", "https://neops.example.com/cms")
    env, sc = make(tmp_repo, text)
    out = problems(env, sc, tmp_repo)
    assert sum("NEOPS_CMS_URL" in p for p in out) == 1


def test_expose_overlay_allows_shared_cms_origin(tmp_repo):
    text = GOOD.replace(
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml",
        "COMPOSE_FILE=compose.yaml:compose.expose.yaml",
    ).replace("https://cms.neops.example.com", "https://neops.example.com")
    env, sc = make(tmp_repo, text)
    assert problems(env, sc, tmp_repo) == []


def test_short_secret_is_flagged(tmp_repo):
    text = GOOD.replace("NEOPS_ADMIN_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f8", "NEOPS_ADMIN_PASSWORD=short1")
    env, sc = make(tmp_repo, text)
    out = problems(env, sc, tmp_repo)
    assert any("NEOPS_ADMIN_PASSWORD" in p and "too short" in p for p in out)


def test_oidc_client_secret_from_external_idp_has_no_length_minimum(tmp_repo):
    text = (
        GOOD.replace("compose.tls-files.yaml", "compose.tls-files.yaml:compose.oidc.yaml")
        + "NEOPS_OIDC_PROVIDER_ID=entra\n"
        + "NEOPS_OIDC_NAME=Company SSO\n"
        + "NEOPS_OIDC_CLIENT_ID=abc\n"
        + "NEOPS_OIDC_DISCOVERY_URL=https://login.example.com/.well-known/openid-configuration\n"
        + "NEOPS_OIDC_CLIENT_SECRET=fourteenchars1\n"
    )
    env, sc = make(tmp_repo, text)
    assert problems(env, sc, tmp_repo) == []


KEYCLOAK_ENV = (
    GOOD.replace(
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml",
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml:"
        "compose.oidc.yaml:compose.keycloak.yaml",
    )
    + "NEOPS_KEYCLOAK_URL=https://auth.neops.example.com\n"
    + "NEOPS_KEYCLOAK_ADMIN_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f9\n"
    + "NEOPS_KEYCLOAK_DB_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5fa\n"
)

METRICS_ENV = (
    GOOD.replace(
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml",
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml:compose.metrics.yaml",
    )
    + "NEOPS_GRAFANA_ADMIN_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5fb\n"
    + "NEOPS_GRAFANA_URL=https://grafana.neops.example.com\n"
)


@pytest.mark.parametrize("base_env", [KEYCLOAK_ENV, METRICS_ENV], ids=["keycloak", "metrics"])
@pytest.mark.parametrize(
    "bad_key,original,bad_value",
    [
        ("NEOPS_WEB_URL", "https://neops.example.com", "neops.example.com"),
        ("NEOPS_CMS_URL", "https://cms.neops.example.com", "https://cms.neops.example.com/?x=1"),
    ],
)
def test_malformed_base_url_does_not_crash_when_an_overlay_adds_a_url(
    tmp_repo, base_env, bad_key, original, bad_value
):
    text = base_env.replace(f"{bad_key}={original}", f"{bad_key}={bad_value}")
    env, sc = make(tmp_repo, text)
    out = problems(env, sc, tmp_repo)
    assert any(bad_key in p for p in out)
