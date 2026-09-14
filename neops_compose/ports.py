from __future__ import annotations

DEFAULT_HTTP_PORT = 80
DEFAULT_HTTPS_PORT = 443
DEFAULT_MONITOR_PORT = 8443
# Traefik's own "monitor" entrypoint always listens on this container-internal
# port; NEOPS_MONITOR_PORT only controls what host port compose publishes it on.
MONITOR_CONTAINER_PORT = 8443
