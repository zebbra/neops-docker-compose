from pathlib import Path

import pytest
import yaml

from neops_compose import override
from neops_compose.env import Env
from neops_compose.override import OverrideError
from neops_compose.render import cms_env
from neops_compose.scenario import BASE_FILE, OVERLAYS, OVERRIDE_FILE, Scenario

REPO = Path(__file__).resolve().parents[2]
CORE_SERVICES = ("cms-init", "cms", "cms-worker", "cms-beat")
COMPOSE_FILE = "compose.yaml:compose.traefik.yaml"


@pytest.fixture
def deployment(tmp_path: Path) -> Path:
    """The shipped compose files and a .env, and no compose.override.yaml of the developer's."""
    for name in (BASE_FILE, *OVERLAYS.values()):
        (tmp_path / name).symlink_to(REPO / name)
    (tmp_path / ".env").write_text(f"# operator comment\nCOMPOSE_FILE={COMPOSE_FILE}\nNEOPS_X=1\n")
    return tmp_path


def environment(repo: Path, service: str) -> dict:
    return yaml.safe_load((repo / OVERRIDE_FILE).read_text())["services"][service]["environment"]


def test_the_core_services_are_those_reading_the_generated_cms_env():
    assert override.core_services(REPO) == CORE_SERVICES


def test_set_writes_the_key_into_every_core_service(deployment):
    override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True"})
    for service in CORE_SERVICES:
        assert environment(deployment, service) == {"CHECK_CLEANUP_ENABLED": "True"}


def test_set_replaces_a_value_and_keeps_the_other_keys(deployment):
    override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True", "CHECK_RETENTION_MAX_ENTRIES": "10"})
    override.set_settings(deployment, {"CHECK_RETENTION_MAX_ENTRIES": "50"})
    assert environment(deployment, "cms-beat") == {
        "CHECK_CLEANUP_ENABLED": "True",
        "CHECK_RETENTION_MAX_ENTRIES": "50",
    }


def test_a_dollar_sign_reaches_core_literally(deployment):
    override.set_settings(deployment, {"SOME_TOKEN": "a$b"})
    assert environment(deployment, "cms")["SOME_TOKEN"] == "a$$b"
    assert override.settings(deployment)["cms"] == {"SOME_TOKEN": "a$b"}


def test_operator_content_survives_a_set(deployment):
    (deployment / OVERRIDE_FILE).write_text(
        "# my own tweaks\n"
        "services:\n"
        "  web:\n"
        "    environment:\n"
        "      FRONTEND_DEVMODE: 'true'  # keep\n"
        "  cms:\n"
        "    mem_limit: 4g\n"
        "    environment:\n"
        "      - MY_FLAG=1\n"
    )
    override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True"})
    text = (deployment / OVERRIDE_FILE).read_text()
    assert "# my own tweaks" in text
    assert "# keep" in text
    doc = yaml.safe_load(text)["services"]
    assert doc["web"]["environment"] == {"FRONTEND_DEVMODE": "true"}
    assert doc["cms"]["mem_limit"] == "4g"
    assert doc["cms"]["environment"] == ["MY_FLAG=1", "CHECK_CLEANUP_ENABLED=True"]


def test_set_replaces_a_key_in_a_list_shaped_environment(deployment):
    (deployment / OVERRIDE_FILE).write_text("services:\n  cms:\n    environment:\n      - A_KEY=1\n")
    override.set_settings(deployment, {"A_KEY": "2"})
    assert environment(deployment, "cms") == ["A_KEY=2"]
    assert override.settings(deployment)["cms"] == {"A_KEY": "2"}


def test_unset_removes_the_key_and_drops_emptied_service_blocks(deployment):
    override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True", "FACT_CLEANUP_INTERVAL_MINUTES": "5"})
    assert override.unset_settings(deployment, ["CHECK_CLEANUP_ENABLED"]) == ["CHECK_CLEANUP_ENABLED"]
    assert environment(deployment, "cms") == {"FACT_CLEANUP_INTERVAL_MINUTES": "5"}
    override.unset_settings(deployment, ["FACT_CLEANUP_INTERVAL_MINUTES"])
    assert yaml.safe_load((deployment / OVERRIDE_FILE).read_text())["services"] == {}


def test_unset_keeps_a_service_that_still_carries_operator_content(deployment):
    (deployment / OVERRIDE_FILE).write_text("services:\n  cms:\n    mem_limit: 4g\n")
    override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True"})
    override.unset_settings(deployment, ["CHECK_CLEANUP_ENABLED"])
    assert yaml.safe_load((deployment / OVERRIDE_FILE).read_text())["services"] == {
        "cms": {"mem_limit": "4g"}
    }


def test_unset_of_an_absent_key_reports_nothing_removed(deployment):
    assert override.unset_settings(deployment, ["NOT_THERE"]) == []
    assert not (deployment / OVERRIDE_FILE).exists()


def test_settings_lists_only_core_services(deployment):
    (deployment / OVERRIDE_FILE).write_text("services:\n  web:\n    environment:\n      X_WEB: '1'\n")
    override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True"})
    assert override.settings(deployment) == {s: {"CHECK_CLEANUP_ENABLED": "True"} for s in CORE_SERVICES}


def test_settings_of_a_missing_override_is_empty(deployment):
    assert override.settings(deployment) == {}


@pytest.mark.parametrize("text", ["services: [\n", "- a\n- b\n", "services: [1, 2]\n"])
def test_an_unusable_override_is_refused_and_left_alone(deployment, text):
    (deployment / OVERRIDE_FILE).write_text(text)
    with pytest.raises(OverrideError, match=OVERRIDE_FILE):
        override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True"})
    assert (deployment / OVERRIDE_FILE).read_text() == text


@pytest.mark.parametrize("item", ["lower_case=1", "1ABC=1", "NO_EQUALS", "=1", "A-B=1"])
def test_assignments_need_an_uppercase_env_var_name(item):
    with pytest.raises(OverrideError):
        override.parse_assignments([item])


def test_assignments_keep_everything_after_the_first_equals_sign():
    assert override.parse_assignments(["A_KEY=x=y", "B_KEY="]) == {"A_KEY": "x=y", "B_KEY": ""}


@pytest.mark.parametrize(
    "key",
    [
        "DATABASE_URL",  # compose.yaml, x-core-env
        "DJANGO_SECRET_KEY",  # compose.yaml, from .env
        "FACT_CLEANUP_ENABLED",  # compose.yaml, from .env
        "NEOPS_ADMIN_PASSWORD",  # compose.yaml, cms-init only
        "RATELIMIT_IP_META_KEY",  # compose.traefik.yaml and generated/cms.env
        "AUTH_PROVIDERS_CONFIG_PATH",  # compose.oidc.yaml
        "NEOPS_OIDC_CLIENT_SECRET",  # a .env secret
        "DJANGO_ALLOWED_HOSTS",  # generated/cms.env
        "SENTRY_DSN",  # generated/cms.env, from .env
    ],
)
def test_keys_the_deployment_already_manages_are_refused(deployment, key):
    assert key in override.managed_keys(deployment)
    with pytest.raises(OverrideError, match=key):
        override.set_settings(deployment, {key: "x"})
    assert not (deployment / OVERRIDE_FILE).exists()


def test_the_cleanup_settings_are_not_managed(deployment):
    managed = override.managed_keys(deployment)
    assert not managed & {
        "CHECK_CLEANUP_ENABLED",
        "CHECK_CLEANUP_INTERVAL_MINUTES",
        "CHECK_RETENTION_MAX_ENTRIES",
    }


@pytest.mark.parametrize("example", sorted((REPO / "examples").glob("*.env")), ids=lambda p: p.name)
def test_every_key_the_generated_cms_env_writes_is_managed(example):
    env = Env(example)
    written = {
        line.split("=", 1)[0]
        for line in cms_env(env, Scenario.from_env(env)).splitlines()
        if line and not line.startswith("#")
    }
    assert written <= override.managed_keys(REPO)


def test_ensure_listed_appends_the_override_once(deployment):
    env = Env(deployment / ".env")
    assert override.ensure_listed(env) is True
    assert override.ensure_listed(Env(deployment / ".env")) is False
    assert Env(deployment / ".env").get("COMPOSE_FILE") == f"{COMPOSE_FILE}:{OVERRIDE_FILE}"
    text = (deployment / ".env").read_text()
    assert text.startswith("# operator comment\n")
    assert "NEOPS_X=1" in text


def test_ensure_listed_honours_the_path_separator(deployment):
    (deployment / ".env").write_text(
        "COMPOSE_PATH_SEPARATOR=;\nCOMPOSE_FILE=compose.yaml;compose.expose.yaml\n"
    )
    override.ensure_listed(Env(deployment / ".env"))
    files = Scenario.from_env(Env(deployment / ".env")).files
    assert files == ("compose.yaml", "compose.expose.yaml", OVERRIDE_FILE)


def test_ensure_listed_leaves_an_existing_position_alone(deployment):
    line = f"COMPOSE_FILE=compose.yaml:{OVERRIDE_FILE}:compose.traefik.yaml\n"
    (deployment / ".env").write_text(line)
    assert override.ensure_listed(Env(deployment / ".env")) is False
    assert (deployment / ".env").read_text() == line


def test_ensure_listed_refuses_without_a_dot_env(tmp_path):
    with pytest.raises(OverrideError, match=r"\.env"):
        override.ensure_listed(Env(tmp_path / ".env"))
    assert not (tmp_path / ".env").exists()


def test_set_creates_the_override_with_a_header_saying_whose_file_it_is(deployment):
    override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True"})
    text = (deployment / OVERRIDE_FILE).read_text()
    assert text.startswith("# ")
    header = text.split("services:", 1)[0]
    assert "gitignored" in header and "./neops config" in header


def test_the_header_is_not_added_to_an_operator_file(deployment):
    (deployment / OVERRIDE_FILE).write_text("services:\n  web:\n    mem_limit: 1g\n")
    override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True"})
    assert (deployment / OVERRIDE_FILE).read_text().startswith("services:\n")


def test_the_override_file_is_gitignored():
    lines = (REPO / ".gitignore").read_text().splitlines()
    assert OVERRIDE_FILE in lines or f"/{OVERRIDE_FILE}" in lines


@pytest.mark.parametrize(
    "text",
    [
        "x-env: &shared\n  A_KEY: '1'\nservices:\n  web:\n    environment: *shared\n"
        "  cms:\n    environment: *shared\n",
        "x-svc: &svc\n  environment:\n    A_KEY: '1'\n"
        "services:\n  web:\n    <<: *svc\n  cms:\n    <<: *svc\n",
    ],
    ids=["aliased environment", "merged service"],
)
def test_an_environment_shared_with_other_services_is_refused(deployment, text):
    (deployment / OVERRIDE_FILE).write_text(text)
    for change in (
        lambda: override.set_settings(deployment, {"NEW_KEY": "v"}),
        lambda: override.unset_settings(deployment, ["A_KEY"]),
    ):
        with pytest.raises(OverrideError, match="cms"):
            change()
    assert (deployment / OVERRIDE_FILE).read_text() == text


def test_a_save_keeps_the_mode_and_writes_through_a_symlink(deployment):
    target = deployment / "kept" / "override.yaml"
    target.parent.mkdir()
    target.write_text("services: {}\n")
    target.chmod(0o600)
    (deployment / OVERRIDE_FILE).symlink_to(target)
    override.set_settings(deployment, {"CHECK_CLEANUP_ENABLED": "True"})
    assert (deployment / OVERRIDE_FILE).is_symlink()
    assert target.stat().st_mode & 0o777 == 0o600
    assert "CHECK_CLEANUP_ENABLED" in target.read_text()
