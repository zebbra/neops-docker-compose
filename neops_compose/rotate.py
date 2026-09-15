from __future__ import annotations

import json
import secrets as pysecrets
from collections.abc import Callable

from neops_compose import secrets, token
from neops_compose.compose import Compose
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.render import KEYCLOAK_CLIENT_ID, KEYCLOAK_REALM, keycloak_relative_path, render
from neops_compose.scenario import Scenario
from neops_compose.state import State
from neops_compose.urls import PublicUrl

CORE_SERVICES = ("cms-init", "cms", "cms-worker", "cms-beat")
DB = {  # which -> (service, role, database, env key, dependants to recreate)
    "cms": (
        "postgres-cms",
        "neops",
        "neops",
        "NEOPS_CMS_DB_PASSWORD",
        CORE_SERVICES + ("postgres-exporter-cms",),
    ),
    "engine": (
        "postgres-engine",
        "postgres",
        "neops-workflow",
        "NEOPS_ENGINE_DB_PASSWORD",
        ("engine", "postgres-exporter-engine"),
    ),
    "keycloak": ("postgres-keycloak", "keycloak", "keycloak", "NEOPS_KEYCLOAK_DB_PASSWORD", ("keycloak",)),
}
WHAT = ("db-password", "admin-password", "secret-key", "jwt", "tls", "token", "keycloak-client")
KCADM = "/opt/keycloak/bin/kcadm.sh"
_SET_PASSWORD_SCRIPT = (
    "import os; from django.contrib.auth import get_user_model; "
    "u = get_user_model().objects.get(username=os.environ['U']); "
    "u.set_password(os.environ['P']); u.save()"
)


class RotateError(RuntimeError):
    pass


def _recreate(compose: Compose, services: tuple[str, ...]) -> None:
    present = set(compose.service_names())
    wanted = [s for s in services if s in present]
    if wanted:
        compose.up(*wanted, force_recreate=True)


def db_password(which: str, env: Env, compose: Compose, log: Callable[[str], None]) -> None:
    service, role, db, key, dependants = DB[which]
    new = pysecrets.token_hex(32)
    log(f"ALTER ROLE {role} on {service}")
    compose.exec(
        service,
        "sh",
        "-c",
        f"psql -v ON_ERROR_STOP=1 -U {role} -d {db} "
        f"-c \"ALTER ROLE {role} WITH PASSWORD '$NEOPS_NEW_PASSWORD'\"",
        env={"PGPASSWORD": env.require(key), "NEOPS_NEW_PASSWORD": new},
    )
    env.set(key, new)
    log(f"{key} updated in .env; recreating {', '.join(dependants)}")
    _recreate(compose, dependants)


def admin_password(env: Env, compose: Compose, new: str, log: Callable[[str], None]) -> None:
    user = env.get("NEOPS_ADMIN_USER", "neops")
    compose.exec(
        "cms",
        "python",
        "manage.py",
        "shell",
        "-c",
        _SET_PASSWORD_SCRIPT,
        env={"U": user, "P": new},
    )
    env.set("NEOPS_ADMIN_PASSWORD", new)
    log(f"password of {user} changed and stored in .env")


def secret_key(env: Env, paths: Paths, compose: Compose, state: State, log: Callable[[str], None]) -> None:
    env.set("DJANGO_SECRET_KEY", pysecrets.token_hex(32))
    log("DJANGO_SECRET_KEY rotated; every session and every static API key is now invalid")
    _recreate(compose, CORE_SERVICES)
    token.rotate_engine_token(compose, env, paths, state, log)


def jwt(paths: Paths, compose: Compose, log: Callable[[str], None]) -> None:
    for name in ("private.pem", "public.pem"):
        (paths.jwt_dir / name).unlink(missing_ok=True)
    secrets.ensure_jwt(paths.jwt_dir)
    log("JWT keypair rotated; every user session ends now")
    _recreate(compose, ("cms", "cms-worker", "cms-beat", "engine"))


def public_hosts(env: Env, scenario: Scenario) -> list[str]:
    keys = ["NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL"]
    if scenario.keycloak:
        keys.append("NEOPS_KEYCLOAK_URL")
    if scenario.metrics and env.is_set("NEOPS_GRAFANA_URL"):
        keys.append("NEOPS_GRAFANA_URL")
    return sorted({PublicUrl.parse(env.require(k)).host for k in keys})


def tls(env: Env, scenario: Scenario, paths: Paths, compose: Compose, log: Callable[[str], None]) -> None:
    if not env.flag("NEOPS_TLS_SELF_SIGNED"):
        raise RotateError(
            "only the self-signed certificate can be rotated here; replace ./certs/*.pem by hand otherwise"
        )
    secrets.ensure_selfsigned(paths.tls_dir, public_hosts(env, scenario), rotate=True)
    log("self-signed certificate re-issued")
    _recreate(compose, ("traefik",))


def _kcadm_login(env: Env, compose: Compose) -> None:
    base = f"http://localhost:8080{keycloak_relative_path(env).rstrip('/')}"
    compose.exec(
        "keycloak",
        "bash",
        "-c",
        f'{KCADM} config credentials --server "$NEOPS_KC_BASE" --realm master '
        '--user admin --password "$NEOPS_KC_ADMIN_PASSWORD"',
        env={
            "NEOPS_KC_BASE": base,
            "NEOPS_KC_ADMIN_PASSWORD": env.require("NEOPS_KEYCLOAK_ADMIN_PASSWORD"),
        },
    )


def _kcadm_client_id(compose: Compose) -> str:
    out = compose.exec(
        "keycloak",
        KCADM,
        "get",
        "clients",
        "-r",
        KEYCLOAK_REALM,
        "-q",
        f"clientId={KEYCLOAK_CLIENT_ID}",
        "--fields",
        "id",
    )
    return json.loads(out)[0]["id"]


def keycloak_client(
    env: Env, scenario: Scenario, paths: Paths, compose: Compose, log: Callable[[str], None]
) -> None:
    new = secrets.ensure_keycloak_client_secret(paths.keycloak_client_env, rotate=True)
    _kcadm_login(env, compose)
    client_id = _kcadm_client_id(compose)
    compose.exec(
        "keycloak",
        "bash",
        "-c",
        f'{KCADM} update clients/{client_id} -r {KEYCLOAK_REALM} -s secret="$NEOPS_KC_CLIENT_SECRET"',
        env={"NEOPS_KC_CLIENT_SECRET": new},
    )
    render(env, scenario, paths)
    compose.exec(
        "cms",
        "python",
        "manage.py",
        "seed_oidc_providers",
        "--config",
        "/etc/neops/providers.json",
        "--force",
    )
    log("Keycloak client secret rotated in Keycloak, generated/providers.json and the CMS")
