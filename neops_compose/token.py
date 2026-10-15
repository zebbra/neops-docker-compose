from __future__ import annotations

import base64
import json

from neops_compose.compose import ComposeError
from neops_compose.context import Ctx
from neops_compose.paths import Paths
from neops_compose.secrets import read_env_value, write_secret

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
_ACCESS_TOKEN_SCRIPT = """
import os
from django.contrib.auth import get_user_model
from neops.core.auth import get_auth, get_permissions, get_token_service
user = get_user_model().objects.filter(username=os.environ['NEOPS_USERNAME']).first()
if user is None:
    print('MISSING')
elif not user.is_active:
    print('INACTIVE')
else:
    permissions = get_permissions()
    token = get_token_service().issue_access_token(
        user_id=user.id,
        username=user.username,
        roles=get_auth().get_roles(user),
        permissions=permissions.get_jwt_permissions(user),
        auth_provider="neops-doctor",
        authz_enforced=permissions.authz_enforced,
    )
    print('TOKEN=%s' % token)
"""
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
    return read_env_value(paths.engine_env, ENV_KEY)


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


def mint_access_token(compose, username: str) -> str:
    """A short-lived engine-usable JWT, issued by the CMS itself: under OIDC no password login exists."""
    try:
        out = compose.exec(
            "cms",
            "python",
            "manage.py",
            "shell",
            "-c",
            _ACCESS_TOKEN_SCRIPT,
            env={"NEOPS_USERNAME": username},
        )
    except ComposeError as exc:
        raise RuntimeError(f"could not run the token script in the cms container: {exc}") from exc
    lines = out.splitlines()
    access_token = next((line[6:] for line in lines if line.startswith("TOKEN=")), "")
    if access_token:
        return access_token
    if "INACTIVE" in lines:
        raise RuntimeError(
            f"NEOPS_ADMIN_USER {username!r} is deactivated in the CMS: reactivate it in the CMS admin "
            "site; core also refuses the engine's static API key while it is"
        )
    if "MISSING" in lines:
        raise _no_such_user(username)
    raise RuntimeError(f"the CMS token script printed no token: {out.strip()[-200:]!r}")


def revoke(compose, key_id_: int) -> None:
    """int() first: the id is interpolated into a Django shell script, never passed through as text."""
    script = _DELETE_SCRIPT.format(key_id=int(key_id_))
    compose.exec("cms", "python", "manage.py", "shell", "-c", script)


def _install(ctx: Ctx, token: str) -> None:
    write_secret(ctx.paths.engine_env, f"{ENV_KEY}={token}\n".encode())
    ctx.state.record_api_key(key_id(token), API_KEY_APP)
    ctx.save_state()
    ctx.log("recreating the engine with the new CMS token")
    ctx.compose.up("engine", force_recreate=True)


def ensure_engine_token(ctx: Ctx) -> bool:
    """Mint only when no valid token exists. Returns True when a token was minted.

    TokenCheckUnavailable propagates: an undetermined check must never mint a second key.
    """
    existing = read_engine_token(ctx.paths)
    if existing and token_is_valid(ctx.compose, existing):
        ctx.log("engine CMS token present and valid")
        return False
    ctx.log("minting a CMS API key for the engine")
    _install(ctx, mint(ctx.compose, ctx.admin_user))
    return True


def rotate_engine_token(ctx: Ctx) -> None:
    old = read_engine_token(ctx.paths)
    _install(ctx, mint(ctx.compose, ctx.admin_user))
    if old:
        try:
            revoke(ctx.compose, key_id(old))
            ctx.log(f"revoked API key {key_id(old)}")
        except Exception as exc:  # the new key is live; an unrevoked old one is reported, not fatal
            ctx.log(f"warning: could not revoke the previous API key: {exc}")
