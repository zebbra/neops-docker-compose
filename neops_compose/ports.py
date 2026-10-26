from __future__ import annotations

from neops_compose.env import Env

DEFAULT_HTTP_PORT = 80
DEFAULT_HTTPS_PORT = 443
DEFAULT_MONITOR_PORT = 8443
DEFAULT_KEYCLOAK_PORT = 8180
DEFAULT_GRAFANA_PORT = 3000
# Traefik's own "monitor" entrypoint always listens on this container-internal
# port; NEOPS_MONITOR_PORT only controls what host port compose publishes it on.
MONITOR_CONTAINER_PORT = 8443
# Every port the bundled Traefik publishes binds here. Unset means compose's own default,
# all interfaces on IPv4 and IPv6, which is what every release before this key did.
PROXY_BIND_ADDRESS = "NEOPS_PROXY_BIND_ADDRESS"


def proxy_bind_address(env: Env) -> str:
    return env.get(PROXY_BIND_ADDRESS) or "0.0.0.0"


def proxy_port_mapping(env: Env, host_port: str | int, container_port: int) -> str:
    bind = env.get(PROXY_BIND_ADDRESS)
    mapping = f"{host_port}:{container_port}"
    return f"{bind}:{mapping}" if bind else mapping
