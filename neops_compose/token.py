from __future__ import annotations

import base64
import json
from collections.abc import Callable

from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.secrets import write_secret
from neops_compose.state import State

API_KEY_APP = "workflow"
API_KEY_DESCRIPTION = "neops-docker-compose: workflow engine"
ENV_KEY = "NEOPS_CMS_TOKEN"
_CHECK_SCRIPT = (
    "import os; from neops.enterprise.auth.static_api_key.api_key import get_api_key_entry; "
    "get_api_key_entry(os.environ['NEOPS_CHECK_TOKEN']); print('VALID')"
)
_PK_SCRIPT = (
    "import os; from django.contrib.auth import get_user_model; "
    "print('PK=%s' % get_user_model().objects.get(username=os.environ['NEOPS_USERNAME']).pk)"
)
_DELETE_SCRIPT = (
    "from neops.enterprise.auth.static_api_key.models import StaticAPIKey; "
    "StaticAPIKey.objects.filter(id={key_id}).delete()"
)


def key_id(token: str) -> int:
    """The StaticAPIKey row id, embedded in the base64(json:signature) token."""
    payload = base64.b64decode(token.encode()).decode().rsplit(":", 1)[0]
    return int(json.loads(payload)["id"])


def read_engine_token(paths: Paths) -> str | None:
    if not paths.engine_env.exists():
        return None
    for line in paths.engine_env.read_text().splitlines():
        if line.startswith(ENV_KEY + "="):
            return line.split("=", 1)[1].strip() or None
    return None


def token_is_valid(compose, token: str) -> bool:
    try:
        out = compose.exec(
            "cms", "python", "manage.py", "shell", "-c", _CHECK_SCRIPT, env={"NEOPS_CHECK_TOKEN": token}
        )
    except Exception:
        return False
    return "VALID" in out


def mint(compose, username: str) -> str:
    out = compose.exec(
        "cms", "python", "manage.py", "shell", "-c", _PK_SCRIPT, env={"NEOPS_USERNAME": username}
    )
    pk = next((line[3:] for line in out.splitlines() if line.startswith("PK=")), "")
    if not pk:
        raise RuntimeError(f"could not resolve the CMS user {username!r}")
    out = compose.exec(
        "cms",
        "python",
        "manage.py",
        "generate_api_key",
        pk,
        API_KEY_APP,
        "--description",
        API_KEY_DESCRIPTION,
    )
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("generate_api_key printed nothing")
    return lines[-1]


def revoke(compose, key_id_: int) -> None:
    compose.exec("cms", "python", "manage.py", "shell", "-c", _DELETE_SCRIPT.format(key_id=key_id_))


def _install(compose, paths: Paths, state: State, token: str, log: Callable[[str], None]) -> None:
    write_secret(paths.engine_env, f"{ENV_KEY}={token}\n".encode())
    state.record_api_key(key_id(token), API_KEY_APP)
    state.save(paths.state_file)
    log("recreating the engine with the new CMS token")
    compose.up("engine", force_recreate=True)


def ensure_engine_token(compose, env: Env, paths: Paths, state: State, log: Callable[[str], None]) -> bool:
    """Mint only when no valid token exists. Returns True when a token was minted."""
    existing = read_engine_token(paths)
    if existing and token_is_valid(compose, existing):
        log("engine CMS token present and valid")
        return False
    log("minting a CMS API key for the engine")
    _install(compose, paths, state, mint(compose, env.get("NEOPS_ADMIN_USER", "neops")), log)
    return True


def rotate_engine_token(compose, env: Env, paths: Paths, state: State, log: Callable[[str], None]) -> None:
    old = read_engine_token(paths)
    new = mint(compose, env.get("NEOPS_ADMIN_USER", "neops"))
    _install(compose, paths, state, new, log)
    if old:
        try:
            revoke(compose, key_id(old))
            log(f"revoked API key {key_id(old)}")
        except Exception as exc:  # the new key is live; an unrevoked old one is reported, not fatal
            log(f"warning: could not revoke the previous API key: {exc}")
