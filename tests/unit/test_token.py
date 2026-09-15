import base64
import json

import pytest

from neops_compose import token
from neops_compose.compose import ComposeError
from neops_compose.context import Ctx
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.state import State

UNREACHABLE = object()


def fake_token(key_id: int) -> str:
    payload = json.dumps({"id": key_id, "app": "workflow", "key": "abc"})
    return base64.b64encode(f"{payload}:signature".encode()).decode()


def test_key_id_is_read_from_the_signed_payload():
    assert token.key_id(fake_token(42)) == 42


class FakeCompose:
    """check=True: the CMS answers VALID, INVALID, nothing at all, or is unreachable."""

    def __init__(self, check=True):
        self.check = check
        self.calls = []

    def exec(self, service, *cmd, env=None):
        self.calls.append((service, cmd, env))
        script = cmd[-1]
        if "get_api_key_entry" in script:
            if self.check is UNREACHABLE:
                raise ComposeError("docker compose exec -T -e NEOPS_CHECK_TOKEN cms failed (1) no container")
            return {True: "VALID\n", False: "INVALID\n"}.get(self.check, self.check)
        if "print('PK=" in script:
            return "PK=3\n"
        if "generate_api_key" in cmd:
            return "Your API Key is ready\n" + fake_token(9) + "\n"
        if "delete()" in script:
            return ""
        raise AssertionError(cmd)

    def up(self, *services, wait=True, force_recreate=False):
        self.calls.append(("up", services, force_recreate))


def make(tmp_path, compose=None) -> Ctx:
    (tmp_path / ".env").write_text("NEOPS_ADMIN_USER=neops\n")
    env = Env(tmp_path / ".env")
    paths = Paths.for_repo(tmp_path, env)
    paths.secrets.mkdir(parents=True)
    return Ctx(
        repo=tmp_path,
        env=env,
        paths=paths,
        scenario=Scenario.from_env(env),
        compose=compose or FakeCompose(),
        state=State(),
        log=lambda m: None,
    )


def test_ensure_is_noop_when_existing_token_is_valid(tmp_path):
    ctx = make(tmp_path, FakeCompose(check=True))
    ctx.paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    assert token.ensure_engine_token(ctx) is False
    assert not any(call[0] == "up" for call in ctx.compose.calls)


def test_ensure_mints_and_recreates_engine_when_missing_or_invalid(tmp_path):
    ctx = make(tmp_path, FakeCompose(check=False))
    ctx.paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    assert token.ensure_engine_token(ctx) is True
    assert ctx.paths.engine_env.read_text() == f"NEOPS_CMS_TOKEN={fake_token(9)}\n"
    assert ctx.state.api_keys[-1]["id"] == 9
    assert ctx.compose.calls[-1] == ("up", ("engine",), True)
    assert State.load(ctx.paths.state_file).api_keys[-1]["id"] == 9


def test_the_minted_key_belongs_to_the_configured_admin_user(tmp_path):
    ctx = make(tmp_path, FakeCompose(check=False))
    ctx.env.set("NEOPS_ADMIN_USER", "operator")
    token.ensure_engine_token(ctx)
    usernames = [call[2]["NEOPS_USERNAME"] for call in ctx.compose.calls if call[0] == "cms" and call[2]]
    assert usernames[-1] == "operator"


def test_token_check_reports_valid_invalid_and_unavailable():
    assert token.token_is_valid(FakeCompose(check=True), fake_token(5)) is True
    assert token.token_is_valid(FakeCompose(check=False), fake_token(5)) is False
    with pytest.raises(token.TokenCheckUnavailable, match="could not ask the CMS"):
        token.token_is_valid(FakeCompose(check=UNREACHABLE), fake_token(5))
    with pytest.raises(token.TokenCheckUnavailable, match="neither VALID nor INVALID"):
        token.token_is_valid(FakeCompose(check="Traceback\n"), fake_token(5))


def test_ensure_never_mints_when_the_check_is_undetermined(tmp_path):
    ctx = make(tmp_path, FakeCompose(check=UNREACHABLE))
    ctx.paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    with pytest.raises(token.TokenCheckUnavailable):
        token.ensure_engine_token(ctx)
    assert ctx.state.api_keys == []
    assert token.read_engine_token(ctx.paths) == fake_token(5)
    assert not any(call[0] == "up" for call in ctx.compose.calls)


def test_mint_explains_an_unreachable_cms(tmp_path):
    class NoCms(FakeCompose):
        def exec(self, service, *cmd, env=None):
            raise ComposeError("docker compose exec -T -e NEOPS_USERNAME cms failed (1) no container")

    with pytest.raises(RuntimeError, match="has cms-init run"):
        token.mint(NoCms(), "neops")


def test_revoke_rejects_a_non_numeric_key_id():
    c = FakeCompose()
    with pytest.raises(ValueError):
        token.revoke(c, "1); import os; os.system('sh')  #")
    assert c.calls == []


def test_rotate_revokes_the_previous_key(tmp_path):
    ctx = make(tmp_path, FakeCompose(check=True))
    ctx.paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    ctx.state.record_api_key(5, "workflow")
    token.rotate_engine_token(ctx)
    scripts = [call[1][-1] for call in ctx.compose.calls if call[0] == "cms"]
    assert any("filter(id=5).delete()" in s for s in scripts)
    assert token.key_id(token.read_engine_token(ctx.paths)) == 9


def test_rotate_keeps_the_new_key_when_revoking_the_old_one_fails(tmp_path):
    """A revoke that cannot run leaves a stale key behind, which is a report, not a rollback:
    the engine is already running on the new one."""
    ctx = make(tmp_path, FakeCompose(check=True))
    ctx.paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    logged: list[str] = []
    ctx.log = logged.append
    ctx.compose.exec = _failing_revoke(ctx.compose)
    token.rotate_engine_token(ctx)
    assert token.key_id(token.read_engine_token(ctx.paths)) == 9
    assert any("could not revoke" in message for message in logged)


def _failing_revoke(compose):
    original = compose.exec

    def exec_(service, *cmd, env=None):
        if "delete()" in cmd[-1]:
            raise ComposeError("no container")
        return original(service, *cmd, env=env)

    return exec_
