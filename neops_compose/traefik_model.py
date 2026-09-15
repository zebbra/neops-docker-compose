from __future__ import annotations

import re
from dataclasses import dataclass

from neops_compose.env import Env
from neops_compose.ports import DEFAULT_HTTPS_PORT, DEFAULT_MONITOR_PORT, MONITOR_CONTAINER_PORT
from neops_compose.routes import CORE_PREFIXES, ENGINE_PUBLIC_WORKER_ROUTES
from neops_compose.scenario import Scenario
from neops_compose.urls import PublicUrl, public_urls

SERVICE_URLS = {
    "web": "http://web:8080",
    "cms": "http://cms:8000",
    "engine": "http://engine:3030",
    "monitor": "http://monitor:80",
    "keycloak": "http://keycloak:8080",
    "grafana": "http://grafana:3000",
}
DENY_MIDDLEWARE = "deny-worker-api"
# TEST-NET-1: never routable, so the allow-list matches nobody and Traefik answers 403.
DENY_RANGE = "192.0.2.1/32"


@dataclass(frozen=True)
class EntryPoint:
    name: str
    address: str
    redirect_to: str | None = None


@dataclass(frozen=True)
class Router:
    name: str
    rule: str
    entrypoint: str
    service: str
    priority: int
    middlewares: tuple[str, ...] = ()
    tls: bool = False


@dataclass(frozen=True)
class TraefikConfig:
    entrypoints: tuple[EntryPoint, ...]
    routers: tuple[Router, ...]
    services: dict[str, str]
    middlewares: dict[str, dict]
    tls_mode: str | None
    acme_email: str = ""


def _host_rule(url: PublicUrl) -> str:
    rule = f"Host(`{url.host}`)"
    if url.path:
        rule += f" && PathPrefix(`{url.path}`)"
    return rule


def worker_deny_rule(host: str, prefix: str) -> str:
    # Traefik's Path/PathPrefix are exact and case-sensitive; Express (the engine)
    # matches case-insensitively and tolerates a trailing slash. One regex covers both.
    alternation = "|".join(ENGINE_PUBLIC_WORKER_ROUTES)
    pattern = f"^{re.escape(prefix)}(?i:/({alternation})/?)$"
    return f"Host(`{host}`) && Method(`POST`) && PathRegexp(`{pattern}`)"


@dataclass(frozen=True)
class _Layout:
    """What every router needs to know beyond its own URL: which entrypoint carries it and
    whether this deployment terminates TLS at all."""

    urls: dict[str, PublicUrl]
    scenario: Scenario
    secure: bool
    monitor_port: int

    def entrypoint_for(self, url: PublicUrl) -> str:
        web = self.urls["NEOPS_WEB_URL"]
        if self.scenario.shared_host and url.port == self.monitor_port and url.host == web.host:
            return "monitor"
        return "websecure" if self.secure else "web"

    def router(
        self, name: str, url: PublicUrl, service: str, priority: int, rule: str = "", mws: tuple = ()
    ) -> Router:
        return Router(
            name, rule or _host_rule(url), self.entrypoint_for(url), service, priority, mws, self.secure
        )


def _entrypoints(scenario: Scenario, secure: bool, https_port: int) -> list[EntryPoint]:
    # Traefik's redirection "to" field has no separate "port" sibling: reference the
    # websecure entrypoint by name at the default port, or target the port directly.
    redirect_to = "websecure" if https_port == DEFAULT_HTTPS_PORT else f":{https_port}"
    entrypoints = [EntryPoint("web", ":80", redirect_to if secure else None)]
    if secure:
        entrypoints.append(EntryPoint("websecure", ":443"))
    if scenario.shared_host:
        entrypoints.append(EntryPoint("monitor", f":{MONITOR_CONTAINER_PORT}"))
    return entrypoints


def _cms_routers(layout: _Layout) -> list[Router]:
    """Shared-hostname mode has no hostname of its own for the CMS, so it claims core's
    prefixes on the web client's host, at a priority that beats the web router."""
    cms = layout.urls["NEOPS_CMS_URL"]
    if not layout.scenario.shared_host:
        return [layout.router("cms", cms, "cms", 10)]
    host = layout.urls["NEOPS_WEB_URL"].host
    return [
        layout.router(
            f"cms-{prefix.strip('/').replace('/', '-').replace('.', '')}",
            cms,
            "cms",
            100,
            rule=f"Host(`{host}`) && PathPrefix(`{prefix}`)",
        )
        for prefix in CORE_PREFIXES
    ]


def _engine_routers(layout: _Layout) -> tuple[list[Router], dict[str, dict]]:
    """Two routers for one service: the ordinary one, and a higher-priority one whose only
    job is to answer 403 on the worker routes, which must never be reachable from outside."""
    engine = layout.urls["NEOPS_ENGINE_URL"]
    middlewares: dict[str, dict] = {DENY_MIDDLEWARE: {"ipAllowList": {"sourceRange": [DENY_RANGE]}}}
    engine_mws: tuple[str, ...] = ()
    if engine.path:
        middlewares["engine-strip"] = {"stripPrefix": {"prefixes": [engine.path]}}
        engine_mws = ("engine-strip",)
    return [
        layout.router("engine", engine, "engine", 10, mws=engine_mws),
        layout.router(
            "engine-deny-worker-api",
            engine,
            "engine",
            1000,
            rule=worker_deny_rule(engine.host, engine.path),
            mws=(DENY_MIDDLEWARE,),
        ),
    ], middlewares


def _core_routers(layout: _Layout) -> tuple[list[Router], dict[str, dict]]:
    """The four services every scenario has."""
    engine, middlewares = _engine_routers(layout)
    return [
        layout.router("web", layout.urls["NEOPS_WEB_URL"], "web", 1),
        *_cms_routers(layout),
        *engine,
        layout.router("monitor", layout.urls["NEOPS_WORKFLOWS_URL"], "monitor", 10),
    ], middlewares


def _service_routers(layout: _Layout) -> list[Router]:
    """The overlay services, routed only when their overlay put a public URL in .env."""
    wanted = (("NEOPS_KEYCLOAK_URL", "keycloak"), ("NEOPS_GRAFANA_URL", "grafana"))
    return [
        layout.router(service, layout.urls[key], service, 10) for key, service in wanted if key in layout.urls
    ]


def build_traefik(env: Env, scenario: Scenario) -> TraefikConfig:
    tls = scenario.tls
    layout = _Layout(
        urls=public_urls(env, scenario),
        scenario=scenario,
        secure=tls is not None,
        monitor_port=int(env.get("NEOPS_MONITOR_PORT", str(DEFAULT_MONITOR_PORT))),
    )
    routers, middlewares = _core_routers(layout)
    routers += _service_routers(layout)
    https_port = int(env.get("NEOPS_HTTPS_PORT", str(DEFAULT_HTTPS_PORT)))
    return TraefikConfig(
        entrypoints=tuple(_entrypoints(scenario, layout.secure, https_port)),
        routers=tuple(routers),
        services={name: SERVICE_URLS[name] for name in sorted({r.service for r in routers})},
        middlewares=dict(sorted(middlewares.items())),
        tls_mode=tls,
        acme_email=env.get("NEOPS_ACME_EMAIL") if tls == "acme" else "",
    )


def static_config(cfg: TraefikConfig) -> dict:
    eps: dict[str, dict] = {}
    for ep in cfg.entrypoints:
        entry: dict = {"address": ep.address}
        if ep.redirect_to:
            entry["http"] = {"redirections": {"entryPoint": {"to": ep.redirect_to, "scheme": "https"}}}
        eps[ep.name] = entry
    out: dict = {
        "entryPoints": eps,
        "providers": {"file": {"filename": "/etc/traefik/dynamic.yml", "watch": True}},
        "api": {"dashboard": False},
        "accessLog": {},
        "log": {"level": "INFO"},
    }
    if cfg.tls_mode == "acme":
        out["certificatesResolvers"] = {
            "letsencrypt": {
                "acme": {
                    "email": cfg.acme_email,
                    "storage": "/acme/acme.json",
                    "httpChallenge": {"entryPoint": "web"},
                }
            }
        }
    return out


def dynamic_config(cfg: TraefikConfig) -> dict:
    routers: dict[str, dict] = {}
    for r in cfg.routers:
        entry: dict = {
            "rule": r.rule,
            "entryPoints": [r.entrypoint],
            "service": r.service,
            "priority": r.priority,
        }
        if r.middlewares:
            entry["middlewares"] = list(r.middlewares)
        if r.tls:
            entry["tls"] = {"certResolver": "letsencrypt"} if cfg.tls_mode == "acme" else {}
        routers[r.name] = entry
    services = {name: {"loadBalancer": {"servers": [{"url": url}]}} for name, url in cfg.services.items()}
    out: dict = {
        "http": {
            "routers": routers,
            "services": services,
            "middlewares": cfg.middlewares,
        }
    }
    if cfg.tls_mode == "files":
        cert = {"certFile": "/etc/traefik/certs/cert.pem", "keyFile": "/etc/traefik/certs/key.pem"}
        out["tls"] = {"certificates": [cert], "stores": {"default": {"defaultCertificate": cert}}}
    return out
