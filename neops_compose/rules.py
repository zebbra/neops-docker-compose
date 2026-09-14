from __future__ import annotations

from pathlib import Path

from neops_compose.env import Env
from neops_compose.routes import CORE_PREFIXES, WEB_RESERVED_PATHS
from neops_compose.scenario import BASE_FILE, OVERLAYS, Scenario
from neops_compose.urls import BadUrl, PublicUrl

PLACEHOLDERS = {"changeme", "change_me", "unsafe", "password", "secret", "xxx"}
BASE_URLS = ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL")
BASE_SECRETS = (
    "NEOPS_CMS_DB_PASSWORD",
    "NEOPS_ENGINE_DB_PASSWORD",
    "DJANGO_SECRET_KEY",
    "NEOPS_ADMIN_PASSWORD",
)
OIDC_KEYS = (
    "NEOPS_OIDC_PROVIDER_ID",
    "NEOPS_OIDC_NAME",
    "NEOPS_OIDC_CLIENT_ID",
    "NEOPS_OIDC_CLIENT_SECRET",
    "NEOPS_OIDC_DISCOVERY_URL",
)
KEYCLOAK_SECRETS = ("NEOPS_KEYCLOAK_ADMIN_PASSWORD", "NEOPS_KEYCLOAK_DB_PASSWORD")
SECRET_HINT = "generate one with: openssl rand -hex 32"


def problems(env: Env, scenario: Scenario, repo: Path) -> list[str]:
    out: list[str] = []
    out += _overlay_problems(scenario, repo)
    out += _required(env, BASE_SECRETS, secret=True)
    urls, url_problems = _parse_urls(env, BASE_URLS)
    out += url_problems
    if scenario.tls == "files" and not env.flag("NEOPS_TLS_SELF_SIGNED"):
        out += _required(env, ("NEOPS_TLS_CERT_FILE", "NEOPS_TLS_KEY_FILE"))
    if scenario.tls == "acme":
        out += _required(env, ("NEOPS_ACME_EMAIL",))
        if env.get("NEOPS_HTTP_PORT", "80") != "80":
            out.append(
                "NEOPS_HTTP_PORT must be 80 with compose.tls-acme.yaml: "
                "the HTTP-01 challenge has no other port"
            )
    if scenario.oidc and not scenario.keycloak:
        out += _required(env, OIDC_KEYS, secret=False)
        out += _required(env, ("NEOPS_OIDC_CLIENT_SECRET",), secret=True)
    if scenario.keycloak:
        out += _required(env, KEYCLOAK_SECRETS, secret=True)
        kc, p = _parse_urls(env, ("NEOPS_KEYCLOAK_URL",))
        out += p
        urls.update(kc)
    if scenario.metrics:
        out += _required(env, ("NEOPS_GRAFANA_ADMIN_PASSWORD",), secret=True)
        if env.is_set("NEOPS_GRAFANA_URL"):
            gf, p = _parse_urls(env, ("NEOPS_GRAFANA_URL",))
            out += p
            urls.update(gf)
    if len(urls) >= len(BASE_URLS):
        out += _url_rules(urls, scenario)
    return out


def _overlay_problems(scenario: Scenario, repo: Path) -> list[str]:
    out = []
    if scenario.files[:1] != (BASE_FILE,):
        out.append(f"COMPOSE_FILE must start with {BASE_FILE}")
    for f in scenario.files:
        if not (repo / f).is_file():
            out.append(f"COMPOSE_FILE names {f}, which does not exist")
    for f in scenario.unknown_files:
        if (repo / f).is_file():
            out.append(
                f"COMPOSE_FILE names {f}, which is not a shipped overlay; "
                "local additions belong in compose.override.yaml"
            )
    if scenario.proxy is None:
        out.append("COMPOSE_FILE must include compose.expose.yaml or compose.traefik.yaml")
    if scenario.has("expose") and scenario.has("traefik"):
        out.append(
            "COMPOSE_FILE includes both compose.expose.yaml and compose.traefik.yaml: choose one, not both"
        )
    if scenario.has("tls-files") and scenario.has("tls-acme"):
        out.append("COMPOSE_FILE must include at most one of compose.tls-files.yaml / compose.tls-acme.yaml")
    for name in ("tls-files", "tls-acme", "shared-host"):
        if scenario.has(name) and not scenario.has("traefik"):
            out.append(f"{OVERLAYS[name]} requires compose.traefik.yaml")
    if scenario.keycloak and not scenario.oidc:
        out.append("compose.keycloak.yaml requires compose.oidc.yaml")
    return out


def _required(env: Env, keys: tuple[str, ...], secret: bool = False) -> list[str]:
    out = []
    for key in keys:
        if not env.is_set(key):
            out.append(f"{key} is required" + (f" ({SECRET_HINT})" if secret else ""))
        elif secret and env.get(key).strip().lower() in PLACEHOLDERS:
            out.append(f"{key} is a placeholder value ({SECRET_HINT})")
    return out


def _parse_urls(env: Env, keys: tuple[str, ...]) -> tuple[dict[str, PublicUrl], list[str]]:
    urls: dict[str, PublicUrl] = {}
    out: list[str] = []
    for key in keys:
        if not env.is_set(key):
            out.append(f"{key} is required (the browser-facing URL)")
            continue
        try:
            urls[key] = PublicUrl.parse(env.get(key))
        except BadUrl as exc:
            out.append(f"{key}: {exc}")
    return urls, out


def _url_rules(urls: dict[str, PublicUrl], scenario: Scenario) -> list[str]:
    out = []
    web, cms = urls["NEOPS_WEB_URL"], urls["NEOPS_CMS_URL"]
    if cms.path:
        out.append(
            "NEOPS_CMS_URL must not have a path: core cannot be served under a prefix "
            "(use the shared-host overlay with the web origin instead)"
        )
    if cms.same_origin(web) and not scenario.shared_host:
        out.append(
            "NEOPS_CMS_URL shares the web client's origin, which needs "
            "compose.traefik-shared-host.yaml in COMPOSE_FILE"
        )
    if not cms.same_origin(web) and scenario.shared_host:
        out.append(
            "compose.traefik-shared-host.yaml is in COMPOSE_FILE but "
            "NEOPS_CMS_URL is not the web client's origin"
        )
    if urls["NEOPS_WORKFLOWS_URL"].same_origin(web):
        out.append(
            "NEOPS_WORKFLOWS_URL must be a different origin (host or port) than NEOPS_WEB_URL: "
            "the web client disables the workflow manager on the same origin"
        )
    reserved = CORE_PREFIXES + WEB_RESERVED_PATHS
    for key in ("NEOPS_ENGINE_URL", "NEOPS_KEYCLOAK_URL", "NEOPS_GRAFANA_URL"):
        u = urls.get(key)
        if u and u.path and any(u.path == r or u.path.startswith(r + "/") for r in reserved):
            out.append(f"{key} path {u.path} is reserved by core or the web client")
        if u and u.same_origin(web) and not u.path:
            out.append(f"{key} shares the web client's origin and needs a path prefix")
    if scenario.proxy == "traefik" and not scenario.shared_host:
        for key, u in urls.items():
            if u.path:
                out.append(
                    f"{key} has a path but hostname-per-service routing is selected; "
                    "paths need compose.traefik-shared-host.yaml"
                )
    return out
