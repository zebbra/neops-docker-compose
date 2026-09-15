from __future__ import annotations

from pathlib import Path

from neops_compose.env import Env
from neops_compose.ports import DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT, DEFAULT_MONITOR_PORT
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
    "NEOPS_OIDC_DISCOVERY_URL",
)
OIDC_CLIENT_SECRET = "NEOPS_OIDC_CLIENT_SECRET"
KEYCLOAK_SECRETS = ("NEOPS_KEYCLOAK_ADMIN_PASSWORD", "NEOPS_KEYCLOAK_DB_PASSWORD")
GRAFANA_ADMIN_PASSWORD = "NEOPS_GRAFANA_ADMIN_PASSWORD"
SECRET_HINT = "generate one with: openssl rand -hex 32"

# Every key that is genuinely secret-shaped: an example must ship it blank, and validating an
# example means filling it with a dummy value first. Used by both the compose-config gate
# (tests/compose_config_check.py) and the example-validation tests (tests/unit/test_examples.py)
# instead of each carrying its own copy of this list. Deliberately not OIDC_KEYS wholesale: the
# provider id, name and discovery URL in that tuple are required but plain config, not secrets,
# and the shipped OIDC examples ship them filled in on purpose.
ALL_SECRET_KEYS = (
    BASE_SECRETS + KEYCLOAK_SECRETS + ("NEOPS_OIDC_CLIENT_ID", OIDC_CLIENT_SECRET, GRAFANA_ADMIN_PASSWORD)
)


class BadPorts(Exception):
    pass


def problems(env: Env, scenario: Scenario, repo: Path) -> list[str]:
    out: list[str] = []
    out += _overlay_problems(scenario, repo)
    out += _required(env, BASE_SECRETS, secret=True, min_length=16)
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
        out += _required(
            env,
            (OIDC_CLIENT_SECRET,),
            secret=True,
            hint="the client secret from your identity provider's client configuration",
        )
    if scenario.keycloak:
        out += _required(env, KEYCLOAK_SECRETS, secret=True, min_length=16)
        kc, p = _parse_urls(env, ("NEOPS_KEYCLOAK_URL",))
        out += p
        urls.update(kc)
    if scenario.metrics:
        out += _required(env, (GRAFANA_ADMIN_PASSWORD,), secret=True, min_length=16)
        if env.is_set("NEOPS_GRAFANA_URL"):
            gf, p = _parse_urls(env, ("NEOPS_GRAFANA_URL",))
            out += p
            urls.update(gf)
    if all(key in urls for key in BASE_URLS):
        try:
            ports = _ports(env)
        except BadPorts as exc:
            out.append(str(exc))
        else:
            out += _url_rules(urls, scenario, *ports)
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


def _required(
    env: Env,
    keys: tuple[str, ...],
    secret: bool = False,
    min_length: int | None = None,
    hint: str = SECRET_HINT,
) -> list[str]:
    out = []
    for key in keys:
        if not env.is_set(key):
            out.append(f"{key} is required" + (f" ({hint})" if secret else ""))
        elif secret and env.get(key).strip().lower() in PLACEHOLDERS:
            out.append(f"{key} is a placeholder value ({hint})")
        elif secret and min_length is not None and len(env.get(key).strip()) < min_length:
            out.append(f"{key} is too short (min {min_length}) ({hint})")
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


def _ports(env: Env) -> tuple[int, int, int]:
    try:
        http_port = int(env.get("NEOPS_HTTP_PORT", str(DEFAULT_HTTP_PORT)))
        https_port = int(env.get("NEOPS_HTTPS_PORT", str(DEFAULT_HTTPS_PORT)))
        monitor_port = int(env.get("NEOPS_MONITOR_PORT", str(DEFAULT_MONITOR_PORT)))
    except ValueError:
        raise BadPorts("NEOPS_HTTP_PORT, NEOPS_HTTPS_PORT and NEOPS_MONITOR_PORT must be integers") from None
    return http_port, https_port, monitor_port


def _url_rules(
    urls: dict[str, PublicUrl], scenario: Scenario, http_port: int, https_port: int, monitor_port: int
) -> list[str]:
    return (
        _cms_rules(urls, scenario)
        + _monitor_rules(urls)
        + _path_rules(urls, scenario)
        + _traefik_rules(urls, scenario, http_port, https_port, monitor_port)
    )


def _cms_rules(urls: dict[str, PublicUrl], scenario: Scenario) -> list[str]:
    web, cms = urls["NEOPS_WEB_URL"], urls["NEOPS_CMS_URL"]
    if cms.path:
        return [
            "NEOPS_CMS_URL must not have a path: core cannot be served under a prefix "
            "(use the shared-host overlay with the web origin instead)"
        ]
    out = []
    if cms.same_origin(web) and not scenario.shared_host and scenario.proxy == "traefik":
        out.append(
            "NEOPS_CMS_URL shares the web client's origin, which needs "
            "compose.traefik-shared-host.yaml in COMPOSE_FILE"
        )
    if not cms.same_origin(web) and scenario.shared_host:
        out.append(
            "compose.traefik-shared-host.yaml is in COMPOSE_FILE but "
            "NEOPS_CMS_URL is not the web client's origin"
        )
    return out


def _monitor_rules(urls: dict[str, PublicUrl]) -> list[str]:
    web = urls["NEOPS_WEB_URL"]
    out = []
    if urls["NEOPS_WORKFLOWS_URL"].same_origin(web):
        out.append(
            "NEOPS_WORKFLOWS_URL must be a different origin (host or port) than NEOPS_WEB_URL: "
            "the web client disables the workflow manager on the same origin"
        )
    return out


def _path_rules(urls: dict[str, PublicUrl], scenario: Scenario) -> list[str]:
    web = urls["NEOPS_WEB_URL"]
    out = []
    reserved = CORE_PREFIXES + WEB_RESERVED_PATHS
    for key in ("NEOPS_ENGINE_URL", "NEOPS_KEYCLOAK_URL", "NEOPS_GRAFANA_URL"):
        u = urls.get(key)
        if u and u.path and any(u.path == r or u.path.startswith(r + "/") for r in reserved):
            out.append(f"{key} path {u.path} is reserved by core or the web client")
        if u and u.same_origin(web) and not u.path:
            out.append(f"{key} shares the web client's origin and needs a path prefix")
    if scenario.proxy == "traefik" and not scenario.shared_host:
        for key, u in urls.items():
            if key == "NEOPS_CMS_URL":
                continue
            if u.path:
                out.append(
                    f"{key} has a path but hostname-per-service routing is selected; "
                    "paths need compose.traefik-shared-host.yaml"
                )
    return out


def _traefik_rules(
    urls: dict[str, PublicUrl], scenario: Scenario, http_port: int, https_port: int, monitor_port: int
) -> list[str]:
    out: list[str] = []
    if scenario.proxy != "traefik":
        return out
    expected_scheme = "https" if scenario.tls else "http"
    for key, u in urls.items():
        if u.scheme != expected_scheme:
            out.append(
                f"{key} must use {expected_scheme}:// with this COMPOSE_FILE "
                f"(TLS overlay {'present' if scenario.tls else 'absent'})"
            )

    web = urls["NEOPS_WEB_URL"]

    if scenario.shared_host:
        conflict_port = https_port if scenario.tls else http_port
        conflict_key = "NEOPS_HTTPS_PORT" if scenario.tls else "NEOPS_HTTP_PORT"
        if monitor_port == conflict_port:
            out.append(
                f"NEOPS_MONITOR_PORT must differ from {conflict_key} in shared-host mode "
                f"({monitor_port}): otherwise Traefik cannot tell the monitor URL apart "
                "from the other shared-host routes on the same port"
            )

    def is_monitor_entrypoint(u: PublicUrl) -> bool:
        return scenario.shared_host and u.host == web.host and u.port == monitor_port

    expected_port = https_port if scenario.tls else http_port
    port_key = "NEOPS_HTTPS_PORT" if scenario.tls else "NEOPS_HTTP_PORT"
    for key, u in urls.items():
        if is_monitor_entrypoint(u):
            continue
        if u.port != expected_port:
            out.append(f"{key} must use port {port_key} ({expected_port})")

    wf = urls["NEOPS_WORKFLOWS_URL"]
    if scenario.shared_host and wf.host == web.host and wf.port != monitor_port:
        out.append(
            f"NEOPS_WORKFLOWS_URL on the web hostname must use port NEOPS_MONITOR_PORT ({monitor_port})"
        )
    return out
