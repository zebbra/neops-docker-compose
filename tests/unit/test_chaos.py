"""The chaos run's .env corruptions are checked here, where no Docker is needed.

tests/e2e/chaos.py asserts that `./neops check` blocks three broken .env files and names each
problem. Those needles are the only link between the chaos run and the rules, and a reworded
message would turn the assertion into a silent pass. These tests hold the two together.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from neops_compose.rules import problems

from .test_rules import GOOD, make

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import chaos  # noqa: E402
import chaos_kit  # noqa: E402
import run_scenario  # noqa: E402


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


def test_set_value_appends_a_key_the_env_lacks():
    assert chaos.set_value("A=1\n", "NEOPS_LOG_RETENTION", "90") == "A=1\nNEOPS_LOG_RETENTION=90\n"


def test_the_log_steps_join_the_default_run_only_with_the_logs_overlay():
    plain = chaos.selected_steps(None, prune=False)
    with_logs = chaos.selected_steps(None, prune=False, logs=True)
    assert with_logs[: len(plain)] == plain
    assert with_logs[len(plain) :] == list(chaos.LOG_STEPS)
    assert "prune" not in with_logs


def test_the_rollback_drill_runs_last_and_the_phase_two_steps_are_absent():
    steps = list(chaos.LOG_STEPS)
    assert steps[-1] == "c14"
    assert not {"c9", "c11", "c13"} & set(steps)


def test_the_store_and_grafana_are_killed_only_where_they_run():
    logs = chaos_kit.Stack(Path("/x"), {"COMPOSE_FILE": LOGS_FILES}, None)
    plain = chaos_kit.Stack(Path("/x"), {"COMPOSE_FILE": "compose.yaml"}, None)
    assert chaos.kill_services(logs)[-2:] == ("victorialogs", "grafana")
    assert "victorialogs" not in chaos.kill_services(plain)


OVERRIDE = """# Written by tests/e2e/run_scenario.py
services:
  elasticsearch:
    environment:
      - cluster.routing.allocation.disk.threshold_enabled=false
"""
LOGS_FILES = "compose.yaml:compose.traefik.yaml:compose.grafana.yaml:compose.logs.yaml:compose.override.yaml"


def test_an_engine_fault_lands_in_the_override_beside_what_is_there():
    text = chaos_kit.with_engine_env(OVERRIDE, {"NEOPS_LOG_URL": "http://engine:3030"})
    data = yaml.safe_load(text)
    assert text.startswith("# Written by tests/e2e/run_scenario.py\n")
    assert data["services"]["engine"]["environment"] == {"NEOPS_LOG_URL": "http://engine:3030"}
    assert data["services"]["elasticsearch"] == yaml.safe_load(OVERRIDE)["services"]["elasticsearch"]


def test_an_engine_fault_keeps_earlier_engine_entries_in_either_form():
    listed = OVERRIDE + "  engine:\n    environment:\n      - A=1\n"
    text = chaos_kit.with_engine_env(listed, {"B": "2"})
    assert yaml.safe_load(text)["services"]["engine"]["environment"] == {"A": "1", "B": "2"}


def test_the_override_wins_over_the_logs_overlay_because_it_is_last():
    """The injection rule rests on this: the harness puts compose.override.yaml after
    compose.logs.yaml, whose hardcoded NEOPS_LOG_URL it has to beat."""
    example = run_scenario.REPO / "examples" / "logs.env"
    values = run_scenario.build_env(example, "logs", run_scenario.Ports(18000), {})
    files = values["COMPOSE_FILE"].split(":")
    assert files[-1] == "compose.override.yaml" and "compose.logs.yaml" in files


def test_drained_means_ok_and_an_empty_outbox():
    assert chaos_kit.drained({"state": "ok", "outbox": {"rows": 0}})
    assert not chaos_kit.drained({"state": "ok", "outbox": {"rows": 3}})
    assert not chaos_kit.drained({"state": "open", "outbox": {"rows": 0}})


class FakeEngine:
    pass


def test_the_load_counts_a_failing_run_instead_of_raising(monkeypatch):
    stack = SimpleNamespace(deployment=None, engine=FakeEngine())
    calls = []

    def run_once(engine, target):
        calls.append(target)
        if len(calls) == 1:
            raise RuntimeError("engine down")
        load._stop.set()
        return SimpleNamespace(succeeded=True)

    monkeypatch.setattr(chaos_kit.workflows, "prepare", lambda deployment, engine: "target")
    monkeypatch.setattr(chaos_kit.workflows, "run_once", run_once)
    monkeypatch.setattr(chaos_kit.time, "sleep", lambda s: None)
    load = chaos_kit.make_load(stack, threads=1)
    load.start()
    result = load.stop()
    assert result.errors == ["RuntimeError: engine down"] and len(result.completed) == 1
