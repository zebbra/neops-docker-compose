"""The chaos run's .env corruptions are checked here, where no Docker is needed.

tests/e2e/chaos.py asserts that `./neops check` blocks three broken .env files and names each
problem. Those needles are the only link between the chaos run and the rules, and a reworded
message would turn the assertion into a silent pass. These tests hold the two together.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from neops_compose.rules import problems

from .test_rules import GOOD, make

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import chaos  # noqa: E402
import chaos_kit  # noqa: E402
import chaos_logs  # noqa: E402
import loadgen  # noqa: E402
import loadgen_driver  # noqa: E402
import logrecords  # noqa: E402
import run_scenario  # noqa: E402
import workflows  # noqa: E402


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


def test_the_rollback_drill_runs_last_and_the_deferred_clock_skew_step_is_absent():
    steps = list(chaos.LOG_STEPS)
    assert steps[-1] == "c14"
    assert "c13" not in steps


def test_the_generator_is_checked_before_any_step_relies_on_it():
    assert list(chaos.LOG_STEPS)[0] == "loadgen"


def test_the_workflow_load_leaves_out_the_steps_that_need_loadgen():
    steps = chaos.selected_steps(None, prune=False, logs=True, load=chaos_kit.WORKFLOW_LOAD)
    assert not set(chaos_logs.LOADGEN_STEPS) & set(steps)
    assert {"c2", "c8", "c10"} <= set(steps)


def test_only_runs_a_loadgen_step_under_the_workflow_load_too():
    assert chaos.selected_steps(["c9"], prune=False, logs=True, load=chaos_kit.WORKFLOW_LOAD) == ["c9"]


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
    stack = SimpleNamespace(deployment=None, engine=FakeEngine(), load=chaos_kit.WORKFLOW_LOAD)
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
    load = chaos_kit.make_load(stack, seconds=0, threads=1)
    load.start()
    result = load.stop()
    assert result.errors == ["RuntimeError: engine down"] and len(result.completed) == 1


def make_clone(tmp_path: Path, project: str = "neops-e2e-logs-18000", override: str | None = None) -> Path:
    (tmp_path / ".env").write_text(f"COMPOSE_FILE=compose.yaml\nCOMPOSE_PROJECT_NAME={project}\n")
    text = run_scenario.OVERRIDE_YAML if override is None else override
    (tmp_path / "compose.override.yaml").write_text(text)
    return tmp_path


def test_a_harness_clone_is_accepted(tmp_path):
    assert chaos.clone_refusal(make_clone(tmp_path), {}) is None


def test_the_harness_names_its_projects_with_the_prefix_the_guard_wants():
    example = run_scenario.REPO / "examples" / "logs.env"
    values = run_scenario.build_env(example, "logs", run_scenario.Ports(18000), {})
    assert values["COMPOSE_PROJECT_NAME"].startswith(run_scenario.E2E_PROJECT_PREFIX)


def test_a_directory_without_an_env_is_refused(tmp_path):
    assert "no .env" in chaos.clone_refusal(tmp_path, {})


def test_a_deployment_that_is_not_a_harness_project_is_refused(tmp_path):
    """The live checkout: its own project name and no harness override."""
    refusal = chaos.clone_refusal(make_clone(tmp_path, project="neops"), {})
    assert "COMPOSE_PROJECT_NAME" in refusal and "neops-e2e-" in refusal


def test_an_override_the_harness_did_not_write_is_refused(tmp_path):
    refusal = chaos.clone_refusal(make_clone(tmp_path, override="services: {}\n"), {})
    assert "compose.override.yaml" in refusal


def test_an_override_with_an_injected_engine_fault_is_still_the_harness_s(tmp_path):
    injected = chaos_kit.with_engine_env(run_scenario.OVERRIDE_YAML, {"NEOPS_LOG_URL": "http://x"})
    assert chaos.clone_refusal(make_clone(tmp_path, override=injected), {}) is None


@pytest.mark.parametrize("key", ["COMPOSE_PROJECT_NAME", "COMPOSE_FILE"])
def test_compose_settings_in_the_shell_are_refused_because_they_beat_the_env(tmp_path, key):
    refusal = chaos.clone_refusal(make_clone(tmp_path), {key: "neops"})
    assert key in refusal


@pytest.mark.parametrize(
    ("inspect", "refused"),
    [("exited 1 0", True), ("restarting 1 3", True), ("running 0 0", False), ("exited 0 0", False)],
)
def test_a_worker_counts_as_refused_when_it_exited_non_zero_or_restarted(inspect, refused):
    assert chaos_logs.parse_worker_exit(inspect)[0] is refused


def test_the_refusal_marker_is_the_compatibility_message_and_no_startup_banner():
    banner = "NEOPS Worker starting... version 0.2.0-beta.8, engine compatibility check"
    assert chaos_logs.REFUSAL_MARKER not in banner.lower()
    refusal = "Client 0.43.0-beta.8 is not compatible with this engine. Update the SDK."
    assert chaos_logs.REFUSAL_MARKER in refusal.lower()


def test_the_outage_push_is_a_valid_worker_push_so_only_the_outage_can_refuse_it():
    import json

    body = json.loads(chaos_logs.outage_push_body("2026-10-28T09:00:00.000Z"))
    (entry,) = body["entries"]
    assert set(entry) == {"seq", "timestamp", "level", "message"} and entry["level"] == "info"


@pytest.mark.parametrize(
    ("version", "gates"),
    [
        ("v0.44.0-beta.1", True),
        ("0.44.0-beta.1", True),
        ("develop", False),
        ("0.43.0-beta.9", False),
        ("", False),
    ],
)
def test_c12a_needs_an_engine_stamped_with_a_0_44_version(version, gates):
    assert chaos_logs.engine_gates_old_clients(version, "0.44.0-beta.1") is gates


def test_the_c12a_gate_follows_the_engine_pin_of_compose_yaml():
    pinned = chaos_logs.pinned_engine_tag((run_scenario.REPO / "compose.yaml").read_text())
    assert chaos_logs.engine_gates_old_clients(f"v{pinned}", pinned)


BURST_RATE = 10
BURST_LINES = 300
# 30 s of lines at 10 a second from t=0, its fault in place from t=3 - the settle, so the outage's
# lines start at 30, and its service back at t=20 + the slack, so the recent ones end at 200. The
# buffer of the two shapes below holds 60 lines, 6 s.
FAULT = chaos_kit.FaultSpan(
    began=3.0 - chaos_logs.FAULT_SETTLE_SECONDS, ended=20.0 + chaos_logs.RECENT_SLACK_SECONDS
)
RECENT_START = 200 - round(BURST_RATE * (chaos_logs.RECENT_SECONDS - chaos_logs.RECENT_SLACK_SECONDS))
OLDEST_DROPPED = frozenset([*range(30), *range(170, BURST_LINES)])
NEWEST_DROPPED = frozenset([*range(90), *range(230, BURST_LINES)])


def burst_outcome(dropped: int = 0) -> loadgen_driver.BurstOutcome:
    summary = loadgen.Summary(worker_id="w", sent=BURST_LINES, dropped=dropped, exit_code=0)
    run = workflows.Run("burst", next(iter(workflows.SUCCEEDED)), 1.0)
    return loadgen_driver.BurstOutcome("w", summary, run, 0, rate=BURST_RATE, started=0.0)


def tally(assigned: int, received: int, kept: frozenset[int], **counts: int) -> loadgen_driver.Accounting:
    return loadgen_driver.Accounting(
        assigned=assigned,
        received=received,
        duplicates=0,
        burst_received=len(kept),
        newest_burst=counts.pop("newest_burst", max(kept)),
        drops_reported=counts.pop("drops_reported", 0),
        burst_indices=kept,
    )


def judged(stored: loadgen_driver.Accounting, check, *args) -> list[str]:
    report = run_scenario.Report()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(chaos_logs, "stored_lines", lambda *_: stored)
        check(report, None, *args)
    return report.failed


def overflow(
    dropped: int,
    assigned: int,
    received: int,
    drops_reported: int,
    newest_burst: int = BURST_LINES - 1,
    kept: frozenset[int] = OLDEST_DROPPED,
    fault: chaos_kit.FaultSpan = FAULT,
) -> list[str]:
    """C9's verdict on a burst of 300 lines whose store tally is the one given."""
    stored = tally(assigned, received, kept, newest_burst=newest_burst, drops_reported=drops_reported)
    return judged(stored, chaos_logs.assert_overflow_accounted, "start", burst_outcome(dropped), fault)


def test_the_outage_window_starts_once_the_fault_has_settled_and_the_recent_one_before_the_return():
    outage, recent = chaos_logs.burst_windows(burst_outcome(), FAULT)
    assert (outage, recent) == (range(30, BURST_LINES), range(RECENT_START, 200))


def test_the_recent_window_ends_at_the_bursts_end_when_the_burst_ended_before_the_return():
    late = chaos_kit.FaultSpan(began=FAULT.began, ended=60.0)
    _, recent = chaos_logs.burst_windows(burst_outcome(), late)
    assert recent.stop == BURST_LINES - round(BURST_RATE * chaos_logs.RECENT_SLACK_SECONDS)


def test_an_overflow_is_accounted_when_the_store_misses_exactly_the_reported_drops():
    assert overflow(dropped=40, assigned=103, received=63, drops_reported=40) == []


def test_an_overflow_fails_when_the_sdk_dropped_its_newest_lines_instead_of_its_oldest():
    failed = overflow(dropped=20, assigned=103, received=83, drops_reported=20, kept=NEWEST_DROPPED)
    assert any("newest outage lines" in message for message in failed), failed
    assert any("only the oldest" in message for message in failed), failed


def test_an_overflow_fails_when_the_engine_was_never_seen_answering_again():
    unseen = chaos_kit.FaultSpan(began=FAULT.began)
    failed = overflow(dropped=40, assigned=103, received=63, drops_reported=40, fault=unseen)
    assert any("answered again" in message for message in failed), failed


def outbox_cap(missing: int, dropped: int, kept: frozenset[int] = OLDEST_DROPPED) -> list[str]:
    """C8's verdict on a burst of 300 lines, the outbox having counted `dropped` rows."""
    outcome = burst_outcome()
    load = chaos_kit.LoadResult(burst=outcome)
    outage = chaos_logs.Outage("start", {}, {}, {"outbox": {"dropped": dropped}}, True, 0.0, load, FAULT)
    stored = tally(BURST_LINES + 3 + missing, BURST_LINES + 3, kept)
    return judged(stored, chaos_logs.assert_outbox_dropped_oldest, outage)


def test_a_capped_outbox_accounts_for_every_line_the_store_misses():
    assert outbox_cap(missing=38026, dropped=38027) == []


def test_a_capped_outbox_fails_when_the_store_misses_more_lines_than_it_dropped():
    failed = outbox_cap(missing=38028, dropped=38027)
    assert any("38028 missing" in message for message in failed), failed


def test_a_capped_outbox_fails_when_it_kept_its_oldest_rows_and_dropped_the_newest():
    failed = outbox_cap(missing=20, dropped=20, kept=NEWEST_DROPPED)
    assert any("only the oldest" in message for message in failed), failed


def test_a_paused_run_without_a_core_unlock_audit_line_is_named():
    audited = logrecords.RunLines(engine=3, worker=2, core_unlock_audit=1, foreign=0)
    silent = logrecords.RunLines(engine=3, worker=2, core_unlock_audit=0, foreign=0)
    assert chaos_logs.runs_without_audit({"a": audited, "b": silent}) == ["b"]


@pytest.mark.parametrize(
    "assigned,received,drops_reported,failing",
    [
        (104, 63, 40, "received 63 = sent 104 - N 40"),
        (103, 63, 39, "report 39 of the 40"),
        (103, 64, 40, "received 64 = sent 103 - N 40"),
    ],
    ids=["seq-hole-no-drop-explains", "drop-count-short", "more-stored-than-numbered"],
)
def test_an_overflow_fails_when_the_counts_disagree(assigned, received, drops_reported, failing):
    failed = overflow(dropped=40, assigned=assigned, received=received, drops_reported=drops_reported)
    assert any(failing in message for message in failed), failed


def test_an_overflow_fails_when_the_newest_burst_line_never_reached_the_store():
    failed = overflow(dropped=40, assigned=98, received=58, drops_reported=40, newest_burst=294)
    assert any("newest burst line 299" in message for message in failed), failed


@pytest.mark.parametrize(
    "health, up", [("healthy", True), ("running", True), ("unhealthy", False), ("absent", False)]
)
def test_a_service_with_or_without_a_healthcheck_counts_as_up(health, up):
    assert chaos_kit.Stack.is_up(SimpleNamespace(health_of=lambda _service: health), "worker") is up
