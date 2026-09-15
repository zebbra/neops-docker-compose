"""The chaos run's .env corruptions are checked here, where no Docker is needed.

tests/e2e/chaos.py asserts that `./neops check` blocks three broken .env files and names each
problem. Those needles are the only link between the chaos run and the rules, and a reworded
message would turn the assertion into a silent pass. These tests hold the two together.
"""

import sys
from pathlib import Path

from neops_compose.rules import problems

from .test_rules import GOOD, make

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import chaos  # noqa: E402


def test_set_value_replaces_only_that_key():
    assert chaos.set_value("A=1\nB=2\n", "B", "9") == "A=1\nB=9\n"


def test_add_overlay_appends_to_compose_file():
    text = chaos.add_overlay("COMPOSE_FILE=compose.yaml:compose.traefik.yaml\n", "compose.expose.yaml")
    assert text == "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.expose.yaml\n"


def test_the_default_run_never_touches_the_host():
    assert "prune" not in chaos.selected_steps(None, prune=False)


def test_prune_replaces_the_plain_restart_rather_than_adding_to_it():
    assert chaos.selected_steps(None, prune=True) == ["kill", "prune", "env", "engine", "database"]


def test_only_wins_over_prune_and_keeps_the_order_given():
    assert chaos.selected_steps(["prune"], prune=False) == ["prune"]
    assert chaos.selected_steps(["env", "kill"], prune=True) == ["env", "kill"]


def test_every_step_name_resolves_to_a_step():
    assert sorted(chaos.steps_by_name(chaos.Report(), stack=None)) == sorted(chaos.STEP_NAMES)


def test_every_chaos_corruption_is_reported_with_the_word_it_asserts(tmp_repo):
    for label, broken, needle in chaos.broken_envs(GOOD):
        env, scenario = make(tmp_repo, broken)
        reported = problems(env, scenario, tmp_repo)
        assert any(needle in problem for problem in reported), f"{label}: {reported}"
