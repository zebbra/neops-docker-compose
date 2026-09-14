from __future__ import annotations

import base64
import json
from collections.abc import Callable

from neops_compose.compose import ComposeError
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.secrets import write_secret
from neops_compose.state import State

API_KEY_APP = "workflow"
API_KEY_DESCRIPTION = "neops-docker-compose: workflow engine"
ENV_KEY = "NEOPS_CMS_TOKEN"
_CHECK_SCRIPT = """
import os
from neops.enterprise.auth.static_api_key.api_key import get_api_key_entry
try:
    get_api_key_entry(os.environ['NEOPS_CHECK_TOKEN'])
except Exception:
    print('INVALID')
else:
    print('VALID')
"""
_PK_SCRIPT = (
    "import os; from django.contrib.auth import get_user_model; "
    "print('PK=%s' % get_user_model().objects.get(username=os.environ['NEOPS_USERNAME']).pk)"
)
_DELETE_SCRIPT = (
    "from neops.enterprise.auth.static_api_key.models import StaticAPIKey; "
    "StaticAPIKey.objects.filter(id={key_id}).delete()"
)


class TokenCheckUnavailable(RuntimeError):
    """The CMS could not answer whether a token is valid, so validity is unknown, not false."""


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
    except ComposeError as exc:
        raise TokenCheckUnavailable(f"could not ask the CMS about the engine token: {exc}") from exc
    if "INVALID" in out:
        return False
    if "VALID" in out:
        return True
    raise TokenCheckUnavailable(
        f"the CMS token check printed neither VALID nor INVALID: {out.strip()!r}; is the cms service running?"
    )


def _no_such_user(username: str) -> RuntimeError:
    return RuntimeError(f"could not resolve the CMS user {username!r}: is the CMS up and has cms-init run?")


def _user_pk(compose, username: str) -> str:
    try:
        out = compose.exec(
            "cms", "python", "manage.py", "shell", "-c", _PK_SCRIPT, env={"NEOPS_USERNAME": username}
        )
    except ComposeError as exc:
        raise _no_such_user(username) from exc
    pk = next((line[3:] for line in out.splitlines() if line.startswith("PK=")), "")
    if not pk:
        raise _no_such_user(username)
    return pk


def mint(compose, username: str) -> str:
    pk = _user_pk(compose, username)
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
    """int() first: the id is interpolated into a Django shell script, never passed through as text."""
    script = _DELETE_SCRIPT.format(key_id=int(key_id_))
    compose.exec("cms", "python", "manage.py", "shell", "-c", script)


def _install(compose, paths: Paths, state: State, token: str, log: Callable[[str], None]) -> None:
    write_secret(paths.engine_env, f"{ENV_KEY}={token}\n".encode())
    state.record_api_key(key_id(token), API_KEY_APP)
    state.save(paths.state_file)
    log("recreating the engine with the new CMS token")
    compose.up("engine", force_recreate=True)


def ensure_engine_token(compose, env: Env, paths: Paths, state: State, log: Callable[[str], None]) -> bool:
    """Mint only when no valid token exists. Returns True when a token was minted.

    TokenCheckUnavailable propagates: an undetermined check must never mint a second key.
    """
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
