from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Database:
    service: str
    role: str
    name: str
    env_key: str
    dump_name: str


DATABASES = (
    Database("postgres-cms", "neops", "neops", "NEOPS_CMS_DB_PASSWORD", "cms.dump"),
    Database("postgres-engine", "postgres", "neops-workflow", "NEOPS_ENGINE_DB_PASSWORD", "engine.dump"),
    Database("postgres-keycloak", "keycloak", "keycloak", "NEOPS_KEYCLOAK_DB_PASSWORD", "keycloak.dump"),
)
BY_KEY = {db.service.removeprefix("postgres-"): db for db in DATABASES}
