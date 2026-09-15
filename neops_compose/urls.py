from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from neops_compose.env import Env
from neops_compose.scenario import Scenario

# The monitor app rejects anything outside this set, and the web client writes
# these values unescaped into a JS literal: keep public URLs plain.
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_PATH_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)*$")
_DEFAULT_PORTS = {"http": 80, "https": 443}
BASE_URLS = ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL")


class BadUrl(ValueError):
    pass


@dataclass(frozen=True)
class PublicUrl:
    scheme: str
    host: str
    port: int
    path: str  # "" or "/a/b", never a trailing slash

    @classmethod
    def parse(cls, raw: str) -> PublicUrl:
        if any(c in raw for c in "\t\r\n"):
            raise BadUrl(f"{raw!r}: control characters are not allowed")
        parts = urlsplit(raw.strip())
        if parts.scheme not in _DEFAULT_PORTS:
            raise BadUrl(f"{raw!r}: scheme must be http or https")
        if parts.username or parts.password:
            raise BadUrl(f"{raw!r}: user info is not allowed")
        if parts.query or parts.fragment or raw.rstrip().endswith("?") or raw.rstrip().endswith("#"):
            raise BadUrl(f"{raw!r}: query strings and fragments are not allowed")
        host = parts.hostname or ""
        if not host or not _HOST_RE.match(host):
            raise BadUrl(f"{raw!r}: hostname is missing or contains disallowed characters")
        path = parts.path.rstrip("/")
        if path and not _PATH_RE.match(path):
            raise BadUrl(f"{raw!r}: path contains disallowed characters")
        try:
            parsed_port = parts.port
        except ValueError as exc:
            raise BadUrl(f"{raw!r}: invalid port") from exc
        port = _DEFAULT_PORTS[parts.scheme] if parsed_port is None else parsed_port
        if port == 0:
            raise BadUrl(f"{raw!r}: invalid port")
        return cls(parts.scheme, host, port, path)

    @property
    def is_default_port(self) -> bool:
        return self.port == _DEFAULT_PORTS[self.scheme]

    @property
    def origin(self) -> str:
        suffix = "" if self.is_default_port else f":{self.port}"
        return f"{self.scheme}://{self.host}{suffix}"

    def same_origin(self, other: PublicUrl) -> bool:
        return self.origin == other.origin

    def __str__(self) -> str:
        return self.origin + self.path


def public_urls(env: Env, scenario: Scenario) -> dict[str, PublicUrl]:
    """The browser-facing URLs this scenario has, which is one rule with three readers: the
    Traefik routers, doctor's probes and the hostnames the self-signed certificate covers.

    Keycloak arrives with its overlay. Grafana only when the operator also routed it: the
    metrics overlay otherwise leaves it on loopback with no public name at all.
    """
    keys = [*BASE_URLS]
    if scenario.keycloak:
        keys.append("NEOPS_KEYCLOAK_URL")
    if scenario.metrics and env.is_set("NEOPS_GRAFANA_URL"):
        keys.append("NEOPS_GRAFANA_URL")
    return {key: PublicUrl.parse(env.require(key)) for key in keys}
