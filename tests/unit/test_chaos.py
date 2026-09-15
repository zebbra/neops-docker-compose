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


def test_every_chaos_corruption_is_reported_with_the_word_it_asserts(tmp_repo):
    for label, broken, needle in chaos.broken_envs(GOOD):
        env, scenario = make(tmp_repo, broken)
        reported = problems(env, scenario, tmp_repo)
        assert any(needle in problem for problem in reported), f"{label}: {reported}"
