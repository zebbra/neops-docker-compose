# tests/unit/test_render.py
import json
import stat

import pytest
import yaml

from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.render import MissingSecret, RenderError, render
from neops_compose.scenario import Scenario

HOSTS = """
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
"""
KEYCLOAK = (
    HOSTS.replace("compose.tls-files.yaml", "compose.tls-files.yaml:compose.oidc.yaml:compose.keycloak.yaml")
    + "NEOPS_KEYCLOAK_URL=https://auth.neops.example.com/sso\n"
)
EXTERNAL = (
    HOSTS.replace("compose.tls-files.yaml", "compose.tls-files.yaml:compose.oidc.yaml")
    + """
NEOPS_OIDC_PROVIDER_ID=entra
NEOPS_OIDC_NAME=Company SSO
NEOPS_OIDC_CLIENT_ID=abc
NEOPS_OIDC_CLIENT_SECRET=s3cr3t
NEOPS_OIDC_DISCOVERY_URL=https://login.example.com/.well-known/openid-configuration
"""
)
EXPOSE_SHARED = """
COMPOSE_FILE=compose.yaml:compose.expose.yaml
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://neops.example.com
NEOPS_ENGINE_URL=https://neops.example.com/engine
NEOPS_WORKFLOWS_URL=https://neops.example.com:8443
"""


def run(tmp_repo, text):
    (tmp_repo / ".env").write_text(text)
    env = Env(tmp_repo / ".env")
    paths = Paths.for_repo(tmp_repo, env)
    paths.secrets.mkdir(parents=True, exist_ok=True)
    (paths.keycloak_client_env).write_text("NEOPS_KEYCLOAK_CLIENT_SECRET=kcsecret\n")
    return render(env, Scenario.from_env(env), paths), paths


def envfile(p):
    lines = p.read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


def test_cms_env_hosts_mode(tmp_repo):
    written, paths = run(tmp_repo, HOSTS)
    cms = envfile(paths.generated / "cms.env")
    assert cms["DJANGO_ALLOWED_HOSTS"] == "cms.neops.example.com,neops.example.com,cms,localhost,127.0.0.1"
    assert cms["CORS_ORIGIN_ALLOW_ALL"] == "True"
    assert cms["ACCOUNT_DEFAULT_HTTP_PROTOCOL"] == "https"
    assert stat.S_IMODE(paths.generated.stat().st_mode) == 0o700
    assert stat.S_IMODE((paths.generated / "cms.env").stat().st_mode) == 0o600
    assert (paths.generated / "traefik" / "traefik.yml").exists()
    dyn = yaml.safe_load((paths.generated / "traefik" / "dynamic.yml").read_text())
    assert dyn["http"]["routers"]["cms"]["rule"] == "Host(`cms.neops.example.com`)"
    assert dyn["tls"]["stores"]["default"]["defaultCertificate"]["certFile"] == "/etc/traefik/certs/cert.pem"
    assert not (paths.generated / "providers.json").exists()


def test_cms_env_shared_origin_has_no_cors_and_no_traefik(tmp_repo):
    written, paths = run(tmp_repo, EXPOSE_SHARED)
    cms = envfile(paths.generated / "cms.env")
    assert cms["DJANGO_ALLOWED_HOSTS"] == "neops.example.com,cms,localhost,127.0.0.1"
    assert "CORS_ORIGIN_ALLOW_ALL" not in cms
    assert not (paths.generated / "traefik").exists()


def test_external_oidc_providers(tmp_repo):
    _, paths = run(tmp_repo, EXTERNAL)
    doc = json.loads((paths.generated / "providers.json").read_text())
    assert doc == {
        "providers": [
            {
                "provider_id": "entra",
                "name": "Company SSO",
                "client_id": "abc",
                "secret": "s3cr3t",
                "settings": {"server_url": "https://login.example.com/.well-known/openid-configuration"},
            }
        ]
    }


def test_keycloak_realm_and_providers(tmp_repo):
    _, paths = run(tmp_repo, KEYCLOAK)
    assert envfile(paths.generated / "keycloak.env") == {"KC_HTTP_RELATIVE_PATH": "/sso"}
    doc = json.loads((paths.generated / "providers.json").read_text())
    p = doc["providers"][0]
    assert p["provider_id"] == "keycloak" and p["client_id"] == "neops-auth" and p["secret"] == "kcsecret"
    assert (
        p["settings"]["server_url"]
        == "http://keycloak:8080/sso/realms/neops/.well-known/openid-configuration"
    )
    realm = json.loads((paths.generated / "keycloak" / "realm.json").read_text())
    client = realm["clients"][0]
    assert realm["realm"] == "neops" and client["clientId"] == "neops-auth" and client["secret"] == "kcsecret"
    assert client["redirectUris"] == ["https://cms.neops.example.com/accounts/oidc/keycloak/login/callback/"]
    assert client["attributes"]["post.logout.redirect.uris"] == "https://neops.example.com/*"
    mapper = client["protocolMappers"][0]
    assert mapper["protocolMapper"] == "oidc-usermodel-client-role-mapper"
    assert mapper["config"]["id.token.claim"] == "true" and mapper["config"]["userinfo.token.claim"] == "true"


def test_render_is_deterministic_and_cleans_stale_files(tmp_repo):
    _, paths = run(tmp_repo, KEYCLOAK)
    first = {p: p.read_bytes() for p in paths.generated.rglob("*") if p.is_file()}
    run(tmp_repo, HOSTS)
    assert not (paths.generated / "keycloak").exists() and not (paths.generated / "providers.json").exists()
    run(tmp_repo, KEYCLOAK)
    second = {p: p.read_bytes() for p in paths.generated.rglob("*") if p.is_file()}
    assert first == second


def test_render_writes_in_place_preserving_inode(tmp_repo):
    (tmp_repo / ".env").write_text(HOSTS)
    env = Env(tmp_repo / ".env")
    paths = Paths.for_repo(tmp_repo, env)
    paths.secrets.mkdir(parents=True, exist_ok=True)
    (paths.generated / "traefik").mkdir(parents=True)
    stale = paths.generated / "traefik" / "dynamic.yml"
    stale.write_text("stale content that render must overwrite, not replace")
    before_ino = stale.stat().st_ino

    render(env, Scenario.from_env(env), paths)

    after = paths.generated / "traefik" / "dynamic.yml"
    assert after.stat().st_ino == before_ino
    assert after.read_text() != "stale content that render must overwrite, not replace"


def test_keycloak_without_client_secret_raises_missing_secret(tmp_repo):
    (tmp_repo / ".env").write_text(KEYCLOAK)
    env = Env(tmp_repo / ".env")
    paths = Paths.for_repo(tmp_repo, env)
    with pytest.raises(MissingSecret, match="neops keys"):
        render(env, Scenario.from_env(env), paths)


def test_keycloak_realm_json_is_world_readable_for_the_container_uid(tmp_repo):
    _, paths = run(tmp_repo, KEYCLOAK)
    assert stat.S_IMODE((paths.generated / "keycloak" / "realm.json").stat().st_mode) == 0o644
    assert stat.S_IMODE((paths.generated / "keycloak").stat().st_mode) == 0o700


def test_render_error_when_docker_created_a_directory_at_a_generated_path(tmp_repo):
    (tmp_repo / ".env").write_text(HOSTS)
    env = Env(tmp_repo / ".env")
    paths = Paths.for_repo(tmp_repo, env)
    paths.secrets.mkdir(parents=True, exist_ok=True)
    (paths.generated / "traefik" / "dynamic.yml").mkdir(parents=True)

    with pytest.raises(RenderError, match="sudo rm -rf"):
        render(env, Scenario.from_env(env), paths)
