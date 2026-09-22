from pathlib import Path

from neops_compose.env import Env
from neops_compose.scenario import Scenario


def scenario_from(tmp_path: Path, text: str) -> Scenario:
    env_file = tmp_path / ".env"
    env_file.write_text(text)
    return Scenario.from_env(Env(env_file))


def test_the_cms_tasks_profile_comes_from_compose_profiles(tmp_path):
    text = "COMPOSE_FILE=compose.yaml:compose.expose.yaml\nCOMPOSE_PROFILES=cms-tasks\n"
    scenario = scenario_from(tmp_path, text)
    assert scenario.files == ("compose.yaml", "compose.expose.yaml")
    assert scenario.profiles == ("cms-tasks",)
    assert scenario.cms_tasks


def test_without_compose_profiles_the_celery_pair_stays_off(tmp_path):
    scenario = scenario_from(tmp_path, "COMPOSE_FILE=compose.yaml:compose.expose.yaml\n")
    assert scenario.profiles == ()
    assert not scenario.cms_tasks


def test_profiles_are_comma_separated_and_trimmed(tmp_path):
    scenario = scenario_from(tmp_path, "COMPOSE_PROFILES= other , cms-tasks\n")
    assert scenario.profiles == ("other", "cms-tasks")
    assert scenario.cms_tasks
