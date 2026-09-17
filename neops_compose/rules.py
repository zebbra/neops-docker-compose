from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from neops_compose.env import Env
from neops_compose.ports import DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT, DEFAULT_MONITOR_PORT
from neops_compose.routes import CORE_PREFIXES, WEB_RESERVED_PATHS
from neops_compose.scenario import BASE_FILE, OVERLAYS, Scenario
from neops_compose.urls import (
    BASE_URLS,
    BadUrl,
    MonitorPlacement,
    PublicUrl,
    monitor_entrypoint_wanted,
    monitor_placement,
)

PLACEHOLDERS = {"changeme", "change_me", "unsafe", "password", "secret", "xxx"}
# Relaxes the strength rules below, never the presence ones. A local deployment wants a login
# an operator can remember; `./neops check`, install and up all warn while it is on.
DISABLE_SECURITY_CHECKS = "NEOPS_DISABLE_SECURITY_CHECKS"
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
SECRET_HINT = "run ./neops secrets, or generate one with: openssl rand -hex 32"

# Every key that is genuinely secret-shaped: an example must ship it blank, and validating an
# example means filling it with a dummy value first. Used by both the compose-config gate
# (tests/compose_config_check.py) and the example-validation tests (tests/unit/test_examples.py)
# instead of each carrying its own copy of this list. Deliberately not OIDC_KEYS wholesale: the
# provider id, name and discovery URL in that tuple are required but plain config, not secrets,
# and the shipped OIDC examples ship them filled in on purpose.
ALL_SECRET_KEYS = (
    BASE_SECRETS + KEYCLOAK_SECRETS + ("NEOPS_OIDC_CLIENT_ID", OIDC_CLIENT_SECRET, GRAFANA_ADMIN_PASSWORD)
)
GENERATED_MIN_LENGTH = 16
# The URLs that may carry a path prefix. NEOPS_CMS_URL never does (see _cms_rules).
PREFIXED_URLS = ("NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL", "NEOPS_KEYCLOAK_URL", "NEOPS_GRAFANA_URL")
MONITOR_SAME_ORIGIN_WHY = (
    " (for example /workflows): the web client treats a bare copy of its own origin as no "
    "workflow manager at all, and only relays its session to a monitor on another origin; "
    "under a path of its origin the monitor reads the session from local storage instead"
)


def generated_secrets(scenario: Scenario) -> tuple[str, ...]:
    """The secrets a scenario needs that nobody else issues: `./neops secrets` mints them and
    `check` demands them. The OIDC client credentials are deliberately absent, they come from
    the identity provider."""
    keys = BASE_SECRETS
    if scenario.keycloak:
        keys += KEYCLOAK_SECRETS
    if scenario.metrics:
        keys += (GRAFANA_ADMIN_PASSWORD,)
    return keys


class BadPorts(Exception):
    pass


@dataclass(frozen=True)
class _Ports:
    http: int
    https: int
    monitor: int
    monitor_declared: bool

    def public(self, scenario: Scenario) -> tuple[str, int]:
        """The one port every URL uses behind Traefik, and the .env key that sets it."""
        if scenario.tls:
            return "NEOPS_HTTPS_PORT", self.https
        return "NEOPS_HTTP_PORT", self.http


def problems(env: Env, scenario: Scenario, repo: Path) -> list[str]:
    out: list[str] = []
    out += _overlay_problems(scenario, repo)
    out += _required(env, generated_secrets(scenario), secret=True, min_length=GENERATED_MIN_LENGTH)
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
        kc, p = _parse_urls(env, ("NEOPS_KEYCLOAK_URL",))
        out += p
        urls.update(kc)
    if scenario.metrics:
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
            out += _url_rules(urls, scenario, ports)
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
    """NEOPS_DISABLE_SECURITY_CHECKS drops the strength rules, never the presence ones: an unset
    secret reaches a container empty, while `./neops secrets` would have filled it properly."""
    relax = secret and env.flag(DISABLE_SECURITY_CHECKS)
    out = []
    for key in keys:
        if not env.is_set(key):
            out.append(f"{key} is required" + (f" ({hint})" if secret else ""))
            continue
        if not secret or relax:
            continue
        value = env.get(key).strip()
        if value.lower() in PLACEHOLDERS:
            out.append(f"{key} is a placeholder value ({hint})")
        elif min_length is not None and len(value) < min_length:
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


def _ports(env: Env) -> _Ports:
    try:
        return _Ports(
            http=int(env.get("NEOPS_HTTP_PORT", str(DEFAULT_HTTP_PORT))),
            https=int(env.get("NEOPS_HTTPS_PORT", str(DEFAULT_HTTPS_PORT))),
            monitor=int(env.get("NEOPS_MONITOR_PORT", str(DEFAULT_MONITOR_PORT))),
            monitor_declared=env.is_set("NEOPS_MONITOR_PORT"),
        )
    except ValueError:
        raise BadPorts("NEOPS_HTTP_PORT, NEOPS_HTTPS_PORT and NEOPS_MONITOR_PORT must be integers") from None


def _url_rules(urls: dict[str, PublicUrl], scenario: Scenario, ports: _Ports) -> list[str]:
    return _cms_rules(urls, scenario) + _path_rules(urls, scenario) + _traefik_rules(urls, scenario, ports)


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


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _path_rules(urls: dict[str, PublicUrl], scenario: Scenario) -> list[str]:
    web = urls["NEOPS_WEB_URL"]
    out = []
    reserved = CORE_PREFIXES + WEB_RESERVED_PATHS
    for key in PREFIXED_URLS:
        u = urls.get(key)
        if u is None:
            continue
        if u.path and any(_under(u.path, r) for r in reserved):
            out.append(f"{key} path {u.path} is reserved by core or the web client")
        if u.same_origin(web) and not u.path:
            why = MONITOR_SAME_ORIGIN_WHY if key == "NEOPS_WORKFLOWS_URL" else ""
            out.append(f"{key} shares the web client's origin and needs a path prefix{why}")
    out += _prefix_collisions(urls)
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


def _prefix_collisions(urls: dict[str, PublicUrl]) -> list[str]:
    """Two services on one origin need prefixes that neither contains the other's, or the
    router with the longer rule takes the other one's requests."""
    prefixed = [(key, urls[key]) for key in PREFIXED_URLS if key in urls and urls[key].path]
    out = []
    for i, (a_key, a) in enumerate(prefixed):
        for b_key, b in prefixed[i + 1 :]:
            if a.same_origin(b) and (_under(a.path, b.path) or _under(b.path, a.path)):
                out.append(f"{a_key} ({a.path}) and {b_key} ({b.path}) overlap on {a.origin}")
    return out


def _traefik_rules(urls: dict[str, PublicUrl], scenario: Scenario, ports: _Ports) -> list[str]:
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
    port_key, expected_port = ports.public(scenario)
    own_entrypoint = monitor_entrypoint_wanted(urls, scenario)
    for key, u in urls.items():
        if key == "NEOPS_WORKFLOWS_URL" and own_entrypoint:
            continue
        if u.port != expected_port:
            out.append(f"{key} must use port {port_key} ({expected_port})")
    if scenario.shared_host:
        out += _monitor_port_rules(urls, scenario, ports)
    return out


def _monitor_port_rules(urls: dict[str, PublicUrl], scenario: Scenario, ports: _Ports) -> list[str]:
    """Shared-host mode: the monitor on the web hostname is either its own entrypoint on
    NEOPS_MONITOR_PORT or a path on the web client's origin, which publishes no port."""
    placement = monitor_placement(urls)
    port_key, public = ports.public(scenario)
    if placement is MonitorPlacement.WEB_PORT:
        if ports.monitor == public:
            return [
                f"NEOPS_MONITOR_PORT must differ from {port_key} in shared-host mode "
                f"({ports.monitor}): otherwise Traefik cannot tell the monitor URL apart "
                "from the other shared-host routes on the same port"
            ]
        if urls["NEOPS_WORKFLOWS_URL"].port != ports.monitor:
            return [
                "NEOPS_WORKFLOWS_URL on the web hostname must use port NEOPS_MONITOR_PORT "
                f"({ports.monitor}), or be the web client's origin plus a path"
            ]
    elif ports.monitor_declared:
        return [
            f"NEOPS_MONITOR_PORT is set, but NEOPS_WORKFLOWS_URL is {placement.value}, "
            "which publishes no monitor port: remove NEOPS_MONITOR_PORT"
        ]
    return []
