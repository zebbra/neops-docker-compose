import base64
import json

from neops_compose import token
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.state import State


def fake_token(key_id: int) -> str:
    payload = json.dumps({"id": key_id, "app": "workflow", "key": "abc"})
    return base64.b64encode(f"{payload}:signature".encode()).decode()


def test_key_id_is_read_from_the_signed_payload():
    assert token.key_id(fake_token(42)) == 42


class FakeCompose:
    def __init__(self, valid: bool):
        self.valid = valid
        self.calls = []

    def exec(self, service, *cmd, env=None):
        self.calls.append((service, cmd, env))
        script = cmd[-1]
        if "get_api_key_entry" in script:
            if self.valid:
                return "VALID\n"
            raise RuntimeError("invalid api key")
        if "print('PK=" in script:
            return "PK=3\n"
        if "generate_api_key" in cmd:
            return "Your API Key is ready\n" + fake_token(9) + "\n"
        if "delete()" in script:
            return ""
        raise AssertionError(cmd)

    def up(self, *services, wait=True, force_recreate=False):
        self.calls.append(("up", services, force_recreate))


def make(tmp_path):
    (tmp_path / ".env").write_text("NEOPS_ADMIN_USER=neops\n")
    env = Env(tmp_path / ".env")
    paths = Paths.for_repo(tmp_path, env)
    paths.secrets.mkdir(parents=True)
    return env, paths


def test_ensure_is_noop_when_existing_token_is_valid(tmp_path):
    env, paths = make(tmp_path)
    paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    c = FakeCompose(valid=True)
    assert token.ensure_engine_token(c, env, paths, State(), log=lambda m: None) is False
    assert not any(call[0] == "up" for call in c.calls)


def test_ensure_mints_and_recreates_engine_when_missing_or_invalid(tmp_path):
    env, paths = make(tmp_path)
    paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    c = FakeCompose(valid=False)
    state = State()
    assert token.ensure_engine_token(c, env, paths, state, log=lambda m: None) is True
    assert paths.engine_env.read_text() == f"NEOPS_CMS_TOKEN={fake_token(9)}\n"
    assert state.api_keys[-1]["id"] == 9
    assert c.calls[-1] == ("up", ("engine",), True)


def test_rotate_revokes_the_previous_key(tmp_path):
    env, paths = make(tmp_path)
    paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    c = FakeCompose(valid=True)
    state = State()
    state.record_api_key(5, "workflow")
    token.rotate_engine_token(c, env, paths, state, log=lambda m: None)
    scripts = [call[1][-1] for call in c.calls if call[0] == "cms"]
    assert any("filter(id=5).delete()" in s for s in scripts)
    assert token.key_id(token.read_engine_token(paths)) == 9
