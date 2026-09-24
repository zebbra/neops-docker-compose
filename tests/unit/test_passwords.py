from pathlib import Path

from neops_compose.env import Env
from neops_compose.passwords import fill_missing
from neops_compose.rules import GENERATED_MIN_LENGTH, generated_secrets, problems
from neops_compose.scenario import Scenario

LOCAL = """COMPOSE_FILE=compose.yaml:compose.expose.yaml
NEOPS_WEB_URL=http://localhost:8080
NEOPS_CMS_URL=http://localhost:8000
NEOPS_ENGINE_URL=http://localhost:3030
NEOPS_WORKFLOWS_URL=http://localhost:3031
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=kept-as-it-is-0123456789
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_PASSWORD=
"""


def make(tmp_path: Path, text: str) -> tuple[Env, Scenario]:
    (tmp_path / ".env").write_text(text)
    env = Env(tmp_path / ".env")
    return env, Scenario.from_env(env)


def test_fills_only_the_blank_generated_secrets(tmp_path):
    env, scenario = make(tmp_path, LOCAL)
    assert fill_missing(env, scenario) == [
        "NEOPS_CMS_DB_PASSWORD",
        "DJANGO_SECRET_KEY",
        "NEOPS_ADMIN_PASSWORD",
    ]
    reread = Env(tmp_path / ".env")
    assert reread.get("NEOPS_ENGINE_DB_PASSWORD") == "kept-as-it-is-0123456789"
    assert reread.get("NEOPS_ADMIN_USER") == "neops"
    for key in ("NEOPS_CMS_DB_PASSWORD", "DJANGO_SECRET_KEY", "NEOPS_ADMIN_PASSWORD"):
        assert len(reread.get(key)) >= GENERATED_MIN_LENGTH, key


def test_filled_env_passes_check_and_a_second_run_changes_nothing(tmp_path, tmp_repo):
    env, scenario = make(tmp_path, LOCAL)
    fill_missing(env, scenario)
    before = (tmp_path / ".env").read_text()
    assert problems(Env(tmp_path / ".env"), scenario, tmp_repo) == []
    assert fill_missing(Env(tmp_path / ".env"), scenario) == []
    assert (tmp_path / ".env").read_text() == before


def test_appends_the_overlay_secrets_the_env_does_not_mention(tmp_path):
    text = LOCAL.replace(
        "compose.expose.yaml",
        "compose.expose.yaml:compose.oidc.yaml:compose.keycloak.yaml:compose.metrics.yaml",
    )
    env, scenario = make(tmp_path, text)
    filled = fill_missing(env, scenario)
    assert filled[-3:] == [
        "NEOPS_KEYCLOAK_ADMIN_PASSWORD",
        "NEOPS_KEYCLOAK_DB_PASSWORD",
        "NEOPS_GRAFANA_ADMIN_PASSWORD",
    ]
    reread = Env(tmp_path / ".env")
    assert all(reread.is_set(key) for key in filled)
    assert not reread.is_set("NEOPS_OIDC_CLIENT_SECRET")


def test_generated_secrets_follow_the_scenario():
    base = Scenario(("compose.yaml", "compose.expose.yaml"))
    assert "NEOPS_KEYCLOAK_DB_PASSWORD" not in generated_secrets(base)
    assert "NEOPS_OIDC_CLIENT_SECRET" not in generated_secrets(
        Scenario(("compose.yaml", "compose.oidc.yaml"))
    )
    assert "NEOPS_GRAFANA_ADMIN_PASSWORD" in generated_secrets(
        Scenario(("compose.yaml", "compose.metrics.yaml"))
    )
