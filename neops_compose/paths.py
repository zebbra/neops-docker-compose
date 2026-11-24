from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from neops_compose.env import Env


@dataclass(frozen=True)
class Paths:
    repo: Path
    data: Path
    # NEOPS_BACKUP_DIR; None is the repo's backups/, the only place before the key existed.
    backup_root: Path | None = None

    @classmethod
    def for_repo(cls, repo: Path, env: Env) -> Paths:
        data = (repo / Path(env.get("NEOPS_DATA_DIR", "./data"))).resolve()
        backups = (repo / Path(env.get("NEOPS_BACKUP_DIR", "./backups"))).resolve()
        return cls(repo=repo.resolve(), data=data, backup_root=backups)

    @property
    def env_file(self) -> Path:
        return self.repo / ".env"

    @property
    def generated(self) -> Path:
        return self.repo / "generated"

    @property
    def backups(self) -> Path:
        """./neops backup's archives and the pre-migrate snapshots of .env and the state file."""
        return self.backup_root or self.repo / "backups"

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
            self.data / "logs" / "victoria",
            self.secrets,
            self.jwt_dir,
            self.tls_dir,
            self.state_file.parent,
        ]

    def private_dirs(self) -> list[Path]:
        """The subset of data_dirs() that must never be group- or world-readable."""
        return [self.secrets, self.jwt_dir, self.tls_dir]
