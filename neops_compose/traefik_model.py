from __future__ import annotations

import re
from dataclasses import dataclass

from neops_compose.env import Env
from neops_compose.ports import DEFAULT_HTTPS_PORT, DEFAULT_MONITOR_PORT, MONITOR_CONTAINER_PORT
from neops_compose.routes import CORE_PREFIXES, ENGINE_PUBLIC_WORKER_ROUTES
from neops_compose.scenario import Scenario
from neops_compose.urls import PublicUrl

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
    redirect_port: int | None = None


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


def build_traefik(env: Env, scenario: Scenario) -> TraefikConfig:
    tls = scenario.tls
    secure = tls is not None
    monitor_port = int(env.get("NEOPS_MONITOR_PORT", str(DEFAULT_MONITOR_PORT)))
    https_port = int(env.get("NEOPS_HTTPS_PORT", str(DEFAULT_HTTPS_PORT)))
    urls = {
        k: PublicUrl.parse(env.require(k))
        for k in ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL")
    }
    if scenario.keycloak:
        urls["NEOPS_KEYCLOAK_URL"] = PublicUrl.parse(env.require("NEOPS_KEYCLOAK_URL"))
    if scenario.metrics and env.is_set("NEOPS_GRAFANA_URL"):
        urls["NEOPS_GRAFANA_URL"] = PublicUrl.parse(env.get("NEOPS_GRAFANA_URL"))

    redirect_port = https_port if secure and https_port != DEFAULT_HTTPS_PORT else None
    entrypoints = [EntryPoint("web", ":80", "websecure" if secure else None, redirect_port)]
    if secure:
        entrypoints.append(EntryPoint("websecure", ":443"))
    if scenario.shared_host:
        entrypoints.append(EntryPoint("monitor", f":{MONITOR_CONTAINER_PORT}"))
    default_ep = "websecure" if secure else "web"

    def entrypoint_for(url: PublicUrl) -> str:
        if scenario.shared_host and url.port == monitor_port and url.host == urls["NEOPS_WEB_URL"].host:
            return "monitor"
        return default_ep

    routers: list[Router] = []
    middlewares: dict[str, dict] = {}
    services: dict[str, str] = {}

    def add(name: str, rule: str, service: str, priority: int, ep: str, mws: tuple[str, ...] = ()) -> None:
        routers.append(Router(name, rule, ep, service, priority, mws, tls=secure))
        services[service] = SERVICE_URLS[service]

    web = urls["NEOPS_WEB_URL"]
    add("web", _host_rule(web), "web", 1, entrypoint_for(web))

    cms = urls["NEOPS_CMS_URL"]
    if scenario.shared_host:
        for prefix in CORE_PREFIXES:
            slug = prefix.strip("/").replace("/", "-").replace(".", "")
            rule = f"Host(`{web.host}`) && PathPrefix(`{prefix}`)"
            add(f"cms-{slug}", rule, "cms", 100, entrypoint_for(cms))
    else:
        add("cms", _host_rule(cms), "cms", 10, entrypoint_for(cms))

    engine = urls["NEOPS_ENGINE_URL"]
    engine_mws: tuple[str, ...] = ()
    if engine.path:
        middlewares["engine-strip"] = {"stripPrefix": {"prefixes": [engine.path]}}
        engine_mws = ("engine-strip",)
    add("engine", _host_rule(engine), "engine", 10, entrypoint_for(engine), engine_mws)
    middlewares[DENY_MIDDLEWARE] = {"ipAllowList": {"sourceRange": [DENY_RANGE]}}
    add(
        "engine-deny-worker-api",
        worker_deny_rule(engine.host, engine.path),
        "engine",
        1000,
        entrypoint_for(engine),
        (DENY_MIDDLEWARE,),
    )

    monitor = urls["NEOPS_WORKFLOWS_URL"]
    add("monitor", _host_rule(monitor), "monitor", 10, entrypoint_for(monitor))

    if "NEOPS_KEYCLOAK_URL" in urls:
        kc = urls["NEOPS_KEYCLOAK_URL"]
        add("keycloak", _host_rule(kc), "keycloak", 10, entrypoint_for(kc))
    if "NEOPS_GRAFANA_URL" in urls:
        gf = urls["NEOPS_GRAFANA_URL"]
        add("grafana", _host_rule(gf), "grafana", 10, entrypoint_for(gf))

    return TraefikConfig(
        entrypoints=tuple(entrypoints),
        routers=tuple(routers),
        services=dict(sorted(services.items())),
        middlewares=dict(sorted(middlewares.items())),
        tls_mode=tls,
        acme_email=env.get("NEOPS_ACME_EMAIL") if tls == "acme" else "",
    )


def static_config(cfg: TraefikConfig) -> dict:
    eps: dict[str, dict] = {}
    for ep in cfg.entrypoints:
        entry: dict = {"address": ep.address}
        if ep.redirect_to:
            redirect_entry: dict = {"to": ep.redirect_to, "scheme": "https"}
            if ep.redirect_port is not None:
                redirect_entry["port"] = str(ep.redirect_port)
            entry["http"] = {"redirections": {"entryPoint": redirect_entry}}
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
