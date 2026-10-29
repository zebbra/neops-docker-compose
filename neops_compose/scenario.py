from __future__ import annotations

from dataclasses import dataclass

from neops_compose.env import Env

BASE_FILE = "compose.yaml"
OVERLAYS: dict[str, str] = {
    "expose": "compose.expose.yaml",
    "traefik": "compose.traefik.yaml",
    "shared-host": "compose.traefik-shared-host.yaml",
    "tls-files": "compose.tls-files.yaml",
    "tls-acme": "compose.tls-acme.yaml",
    "oidc": "compose.oidc.yaml",
    "keycloak": "compose.keycloak.yaml",
    "grafana": "compose.grafana.yaml",
    "metrics": "compose.metrics.yaml",
    "logs": "compose.logs.yaml",
}
OVERRIDE_FILE = "compose.override.yaml"
CMS_TASKS_PROFILE = "cms-tasks"


def _split(raw: str, sep: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(sep) if part.strip())


@dataclass(frozen=True)
class Scenario:
    files: tuple[str, ...]
    profiles: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Env) -> Scenario:
        sep = env.get("COMPOSE_PATH_SEPARATOR", ":")
        files = _split(env.get("COMPOSE_FILE", BASE_FILE), sep)
        profiles = _split(env.get("COMPOSE_PROFILES", ""), ",")
        return cls(files, profiles)

    def has(self, overlay: str) -> bool:
        return OVERLAYS[overlay] in self.files

    @property
    def proxy(self) -> str | None:
        if self.has("traefik"):
            return "traefik"
        if self.has("expose"):
            return "expose"
        return None

    @property
    def tls(self) -> str | None:
        if self.has("tls-files"):
            return "files"
        if self.has("tls-acme"):
            return "acme"
        return None

    @property
    def shared_host(self) -> bool:
        return self.has("shared-host")

    @property
    def oidc(self) -> bool:
        return self.has("oidc")

    @property
    def keycloak(self) -> bool:
        return self.has("keycloak")

    @property
    def grafana(self) -> bool:
        return self.has("grafana")

    @property
    def metrics(self) -> bool:
        return self.has("metrics")

    @property
    def logs(self) -> bool:
        return self.has("logs")

    @property
    def runs_grafana(self) -> bool:
        """compose.metrics.yaml still carries a complete grafana for one release, so a metrics
        .env that migration 0003 has not reached yet runs Grafana too. Drop the metrics half
        together with that duplicate block."""
        return self.grafana or self.metrics

    @property
    def cms_tasks(self) -> bool:
        """Core's Celery worker and beat, the 1.0 task path, start only under this profile."""
        return CMS_TASKS_PROFILE in self.profiles

    @property
    def unknown_files(self) -> tuple[str, ...]:
        known = {BASE_FILE, OVERRIDE_FILE, *OVERLAYS.values()}
        return tuple(f for f in self.files if f not in known)
