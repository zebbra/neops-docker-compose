from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from neops_compose.env import Env


@dataclass(frozen=True)
class Paths:
    repo: Path
    data: Path

    @classmethod
    def for_repo(cls, repo: Path, env: Env) -> Paths:
        raw = env.get("NEOPS_DATA_DIR", "./data")
        data = (repo / Path(raw)).resolve()
        return cls(repo=repo.resolve(), data=data)

    @property
    def env_file(self) -> Path:
        return self.repo / ".env"

    @property
    def generated(self) -> Path:
        return self.repo / "generated"

    @property
    def backups(self) -> Path:
        return self.repo / "backups"

    @property
    def certs(self) -> Path:
        return self.repo / "certs"

    @property
    def cust_certs(self) -> Path:
        """Certificates the operator wants inside the containers' trust store."""
        return self.repo / "cust-cert"

    @property
    def migrations(self) -> Path:
        return self.repo / "migrations"

    @property
    def secrets(self) -> Path:
        return self.data / "secrets"

    @property
    def jwt_dir(self) -> Path:
        return self.secrets / "jwt"

    @property
    def engine_env(self) -> Path:
        return self.secrets / "engine.env"

    @property
    def keycloak_client_env(self) -> Path:
        return self.secrets / "keycloak-client.env"

    @property
    def tls_dir(self) -> Path:
        return self.secrets / "tls"

    @property
    def state_file(self) -> Path:
        return self.data / ".neops" / "state.json"

    def data_dirs(self) -> list[Path]:
        """Every bind-mounted directory the base stack and the overlays use."""
        return [
            self.data / "cms" / "postgres",
            self.data / "cms" / "media",
            self.data / "cms" / "tmp",
            self.data / "engine" / "postgres",
            self.data / "keycloak" / "postgres",
            self.data / "elasticsearch",
            self.data / "traefik" / "acme",
            self.data / "metrics" / "victoria",
            self.data / "metrics" / "grafana",
            self.secrets,
            self.jwt_dir,
            self.tls_dir,
            self.state_file.parent,
        ]

    def private_dirs(self) -> list[Path]:
        """The subset of data_dirs() that must never be group- or world-readable."""
        return [self.secrets, self.jwt_dir, self.tls_dir]
