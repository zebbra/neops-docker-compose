"""The e2e harness's clone handling is checked here, where no Docker is needed.

A rerun with --keep or an existing --workdir reuses the clone, and it must test the commit the
checkout is on now, not the one it was cloned at.
"""

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from neops_compose.doctor import DENY_LOG_PROBE, DENY_PROBE
from neops_compose.env import Env
from neops_compose.rules import problems
from neops_compose.scenario import OVERRIDE_FILE, Scenario

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import run_scenario  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    cmd = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    commit(repo, "first")
    monkeypatch.setattr(run_scenario, "REPO", repo)
    return repo


def test_a_fresh_clone_is_on_the_checkout_head(source: Path, tmp_path: Path):
    head = git(source, "rev-parse", "HEAD")
    clone = run_scenario.prepare_clone(tmp_path / "work", {})
    assert git(clone, "rev-parse", "HEAD") == head


def test_a_reused_clone_moves_to_the_new_checkout_head(source: Path, tmp_path: Path):
    work = tmp_path / "work"
    run_scenario.prepare_clone(work, {})
    head = commit(source, "second")
    clone = run_scenario.prepare_clone(work, {"A": "1"})
    assert git(clone, "rev-parse", "HEAD") == head
    assert (clone / ".env").read_text() == "A=1\n"


def test_an_upgrade_run_starts_on_the_older_ref_and_moves_to_the_checkout_head(source: Path, tmp_path: Path):
    """--upgrade-from: the operator's .env written for the old release stays across the pull."""
    first = git(source, "rev-parse", "HEAD")
    head = commit(source, "second")
    clone = run_scenario.prepare_clone(tmp_path / "work", {"A": "1"}, rev=first)
    assert git(clone, "rev-parse", "HEAD") == first
    run_scenario.move_to_head(clone)
    assert git(clone, "rev-parse", "HEAD") == head
    assert (clone / ".env").read_text() == "A=1\n"


LOKI = run_scenario.E2E_EXAMPLES / "loki.env"
LOKI_OVERLAY = "tests/e2e/compose.loki-test.yaml"


def test_a_scenario_is_a_shipped_example_an_e2e_example_or_a_path():
    assert run_scenario.example_path("logs") == run_scenario.REPO / "examples" / "logs.env"
    assert run_scenario.example_path("loki") == LOKI
    assert run_scenario.example_path("tests/e2e/examples/loki.env") == LOKI


def test_the_overlay_key_is_taken_out_of_the_env():
    values = {"A": "1", run_scenario.HARNESS_OVERLAYS: f"{LOKI_OVERLAY}:other.yaml"}
    assert run_scenario.pop_overlays(values) == [LOKI_OVERLAY, "other.yaml"]
    assert values == {"A": "1"}


def test_merge_merges_mappings_and_replaces_everything_else():
    base = {"services": {"es": {"environment": ["a"]}, "engine": {"image": "x"}}}
    overlay = {"services": {"engine": {"networks": ["logs"]}, "es": {"environment": ["b"]}}}
    assert run_scenario.merge(base, overlay) == {
        "services": {"es": {"environment": ["b"]}, "engine": {"image": "x", "networks": ["logs"]}}
    }


def test_without_overlays_the_override_is_the_harness_fragment():
    assert run_scenario.override_text([]) == run_scenario.OVERRIDE_YAML


def test_the_loki_overlay_reaches_compose_through_the_override_file():
    merged = yaml.safe_load(run_scenario.override_text([LOKI_OVERLAY]))
    services = merged["services"]
    assert services["loki"]["image"].startswith("grafana/loki:")
    assert services["loki"]["networks"] == ["logs"]
    assert "logs" in services["engine"]["networks"] and "worker" not in services
    assert (
        services["elasticsearch"] == yaml.safe_load(run_scenario.OVERRIDE_YAML)["services"]["elasticsearch"]
    )


def test_the_engine_starts_once_loki_is_ready_as_an_operators_running_loki_would_be():
    services = yaml.safe_load(run_scenario.override_text([LOKI_OVERLAY]))["services"]
    assert "http://localhost:3100/ready" in services["loki"]["healthcheck"]["test"]
    assert services["engine"]["depends_on"]["loki"] == {"condition": "service_healthy"}


def test_the_loki_scenario_passes_check_without_the_logs_overlay(tmp_path: Path):
    values = run_scenario.build_env(LOKI, "loki", run_scenario.Ports(22500), {})
    overlays = run_scenario.pop_overlays(values)
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    (tmp_path / OVERRIDE_FILE).write_text(run_scenario.override_text(overlays))
    for f in run_scenario.REPO.glob("compose*.yaml"):
        (tmp_path / f.name).symlink_to(f)
    env = Env(env_file)
    scenario = Scenario.from_env(env)
    assert overlays == [LOKI_OVERLAY]
    assert not scenario.logs and env.get("NEOPS_LOG_BACKEND") == "loki"
    assert problems(env, scenario, tmp_path) == []


@pytest.mark.parametrize(
    ("warned", "older", "ok"),
    [
        ([DENY_PROBE, DENY_LOG_PROBE], False, True),
        ([DENY_PROBE], False, False),
        ([DENY_PROBE], True, True),
        ([DENY_PROBE, DENY_LOG_PROBE], True, True),
        (["engine log store"], True, False),
    ],
)
def test_an_older_release_may_warn_about_the_job_routes_alone(warned, older, ok):
    assert run_scenario.deny_warnings_ok(warned, older) is ok


@pytest.mark.parametrize(
    ("files", "warnings", "after"),
    [
        (
            ("compose.yaml", "compose.traefik.yaml", "compose.metrics.yaml", OVERRIDE_FILE),
            ["compose.grafana.yaml"],
            (
                "compose.yaml",
                "compose.traefik.yaml",
                "compose.grafana.yaml",
                "compose.metrics.yaml",
                OVERRIDE_FILE,
            ),
        ),
        (
            ("compose.yaml", "compose.expose.yaml", OVERRIDE_FILE),
            [],
            ("compose.yaml", "compose.expose.yaml", OVERRIDE_FILE),
        ),
    ],
)
def test_the_upgrade_oracle_per_shape(files, warnings, after):
    """The live `local` shape upgrades without a warning and keeps COMPOSE_FILE as it was."""
    assert run_scenario.expected_upgrade_warnings(Scenario(files)) == warnings
    assert run_scenario.expected_files_after_upgrade(files) == after


def test_warnings_match_needs_each_expected_warning_and_no_other():
    line = "WARNING: compose.metrics.yaml requires compose.grafana.yaml; migration 0003 adds it"
    assert run_scenario.warnings_match([line], ["compose.grafana.yaml"])
    assert not run_scenario.warnings_match([line], [])
    assert not run_scenario.warnings_match([], ["compose.grafana.yaml"])
    assert run_scenario.warnings_match([], [])


@pytest.mark.parametrize(
    "values",
    [
        {"COMPOSE_FILE": "compose.yaml:compose.grafana.yaml:compose.logs.yaml"},
        {"COMPOSE_FILE": "compose.yaml:compose.expose.yaml", "NEOPS_LOG_BACKEND": "loki"},
        {"COMPOSE_FILE": "compose.yaml:compose.expose.yaml"},
    ],
)
def test_every_log_check_a_store_asks_for_has_an_assertion(values):
    import logrecords

    from neops_compose.doctor import Http

    assertions = run_scenario.log_assertions(run_scenario.Report(), Path("/x"), values, Http())
    assert set(logrecords.log_checks(values)) <= set(assertions)


@pytest.mark.parametrize("key", ["COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "COMPOSE_ENV_FILES"])
def test_a_scenario_refuses_compose_settings_in_the_shell(tmp_path, key):
    assert key in run_scenario.shell_compose_refusal({key: "neops-e2e-sentinel"}, tmp_path)


def test_a_clean_shell_is_no_reason_to_refuse(tmp_path):
    assert run_scenario.shell_compose_refusal({"PATH": "/usr/bin"}, tmp_path) is None


def test_main_stops_before_cloning_when_the_shell_names_a_compose_project(tmp_path, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("the shell guard let main() reach a clone or a compose call")

    monkeypatch.setattr(run_scenario, "prepare_clone", must_not_run)
    monkeypatch.setattr(run_scenario, "neops", must_not_run)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "neops-e2e-sentinel-not-a-real-project")
    assert run_scenario.main(["local", "--workdir", str(tmp_path / "work")]) == 2
    assert not (tmp_path / "work").exists()


def test_extra_env_cannot_point_a_scenario_at_another_project():
    with pytest.raises(SystemExit, match="COMPOSE_PROJECT_NAME"):
        run_scenario.build_env(
            run_scenario.chosen_example("local"),
            "local",
            run_scenario.Ports(18000),
            {"COMPOSE_PROJECT_NAME": "some-other-project"},
        )
