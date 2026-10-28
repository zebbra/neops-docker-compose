import stat
from pathlib import Path

import pytest

from neops_compose import migrate
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.state import State


def write_migration(d: Path, name: str, body: str) -> None:
    (d / f"{name}.py").write_text(f'DESCRIPTION = "{name}"\nIDEMPOTENT = True\n\n{body}\n')


def make(tmp_path):
    (tmp_path / ".env").write_text("A=1\n")
    env = Env(tmp_path / ".env")
    paths = Paths.for_repo(tmp_path, env)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "backups").mkdir()
    return env, paths


def test_discover_orders_and_validates_names(tmp_path):
    env, paths = make(tmp_path)
    write_migration(paths.migrations, "0002_second", "def apply(ctx): pass")
    write_migration(paths.migrations, "0001_first", "def apply(ctx): pass")
    assert [m.name for m in migrate.discover(paths.migrations)] == ["0001_first", "0002_second"]
    (paths.migrations / "bad_name.py").write_text("def apply(ctx): pass\n")
    with pytest.raises(migrate.MigrationError, match="bad_name"):
        migrate.discover(paths.migrations)


def test_apply_all_runs_pending_in_order_records_state_and_snapshots(tmp_path):
    env, paths = make(tmp_path)
    write_migration(
        paths.migrations, "0001_a", "def apply(ctx):\n    ctx.mkdir(ctx.data / 'made'); ctx.env.set('B', '2')"
    )
    write_migration(paths.migrations, "0002_b", "def apply(ctx):\n    ctx.log('hello')")
    logs = []
    state = State()
    done = migrate.apply_all(env, paths, state, log=logs.append)
    assert done == ["0001_a", "0002_b"]
    assert (paths.data / "made").is_dir() and Env(paths.env_file).get("B") == "2"
    assert State.load(paths.state_file).applied_names == ["0001_a", "0002_b"]
    snapshots = list(paths.backups.glob("pre-migrate-*"))
    assert len(snapshots) == 1 and (snapshots[0] / ".env").exists()
    assert migrate.apply_all(env, paths, State.load(paths.state_file), log=logs.append) == []


def test_snapshot_is_private_because_env_and_state_hold_secrets(tmp_path):
    env, paths = make(tmp_path)
    State().save(paths.state_file)
    target = migrate.snapshot(paths)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.backups.stat().st_mode) == 0o700
    copied = sorted(p.name for p in target.iterdir())
    assert copied == [".env", "state.json"]
    for name in copied:
        assert stat.S_IMODE((target / name).stat().st_mode) == 0o600, name


def test_failed_migration_is_not_recorded_and_non_idempotent_blocks_rerun(tmp_path):
    env, paths = make(tmp_path)
    (paths.migrations / "0001_boom.py").write_text(
        'DESCRIPTION = "boom"\nIDEMPOTENT = False\n\ndef apply(ctx):\n    raise RuntimeError("disk full")\n'
    )
    with pytest.raises(migrate.MigrationError, match="0001_boom"):
        migrate.apply_all(env, paths, State(), log=lambda m: None)
    assert State.load(paths.state_file).applied_names == []
    marker = paths.state_file.parent / "0001_boom.failed"
    assert marker.exists()
    with pytest.raises(migrate.MigrationError, match="not idempotent"):
        migrate.apply_all(env, paths, State.load(paths.state_file), log=lambda m: None)


def test_fake_requires_full_name_and_is_recorded_separately(tmp_path):
    env, paths = make(tmp_path)
    write_migration(paths.migrations, "0001_a", "def apply(ctx): raise RuntimeError('never')")
    with pytest.raises(migrate.MigrationError, match="0009_nope"):
        migrate.fake("0009_nope", paths, State())
    migrate.fake("0001_a", paths, State())
    s = State.load(paths.state_file)
    assert s.applied_names == ["0001_a"] and s.faked_names == ["0001_a"]
    assert migrate.apply_all(env, paths, s, log=lambda m: None) == []


def test_initial_layout_creates_the_tree(tmp_path, monkeypatch):
    env, paths = make(tmp_path)
    real = Path(__file__).resolve().parents[2] / "migrations" / "0001_initial_layout.py"
    (paths.migrations / real.name).write_text(real.read_text())
    chowns = []
    monkeypatch.setattr(
        migrate.Ctx, "chown_via_container", lambda self, p, uid, gid: chowns.append((p, uid, gid))
    )
    migrate.apply_all(env, paths, State(), log=lambda m: None)
    for d in paths.data_dirs():
        assert d.is_dir(), d
    assert oct(paths.secrets.stat().st_mode & 0o777) == "0o700"
    assert chowns == [(paths.data / "elasticsearch", 1000, 0)]


def test_ensure_owner_chowns_only_when_the_owner_is_wrong(tmp_path, monkeypatch):
    chowns = []
    monkeypatch.setattr(
        migrate.Ctx, "chown_via_container", lambda self, p, uid, gid: chowns.append((p, uid, gid))
    )
    env, paths = make(tmp_path)
    ctx = migrate.Ctx(tmp_path, paths.data, env, log=lambda m: None)
    target = tmp_path / "mount"
    target.mkdir()
    info = target.stat()
    ctx.ensure_owner(target, info.st_uid, info.st_gid)
    assert chowns == []
    ctx.ensure_owner(tmp_path / "absent", 472, 0)
    assert chowns == []
    ctx.ensure_owner(target, info.st_uid + 1, 0)
    assert chowns == [(target, info.st_uid + 1, 0)]


def test_grafana_data_owner_hands_the_mount_to_the_grafana_uid(tmp_path, monkeypatch):
    env, paths = make(tmp_path)
    real = Path(__file__).resolve().parents[2] / "migrations"
    for name in ("0001_initial_layout.py", "0002_grafana_data_owner.py"):
        (paths.migrations / name).write_text((real / name).read_text())
    chowns = []
    monkeypatch.setattr(
        migrate.Ctx, "chown_via_container", lambda self, p, uid, gid: chowns.append((p, uid, gid))
    )
    migrate.apply_all(env, paths, State(), log=lambda m: None)
    assert chowns == [(paths.data / "elasticsearch", 1000, 0), (paths.data / "metrics" / "grafana", 472, 0)]


REAL_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
GRAFANA_AND_LOGS = "0003_grafana_and_logs_layout"


def run_0003(tmp_path, env_text: str) -> tuple[Env, Paths, list[str]]:
    """Migration 0003 alone against `env_text`, as `./neops up` runs it after a `git pull`."""
    (tmp_path / ".env").write_text(env_text)
    env = Env(tmp_path / ".env")
    paths = Paths.for_repo(tmp_path, env)
    (tmp_path / "migrations").mkdir()
    name = f"{GRAFANA_AND_LOGS}.py"
    (paths.migrations / name).write_text((REAL_MIGRATIONS / name).read_text())
    logs: list[str] = []
    migrate.apply_all(env, paths, State(), log=logs.append)
    return Env(tmp_path / ".env"), paths, logs


METRICS_DOTENV = (
    "# the operator's own note\n"
    "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.metrics.yaml\n"
    "NEOPS_GRAFANA_ADMIN_PASSWORD=kept\n"
)


def test_0003_puts_the_grafana_overlay_before_the_metrics_overlay_and_keeps_comments(tmp_path):
    env, _, _ = run_0003(tmp_path, METRICS_DOTENV)
    assert env.get("COMPOSE_FILE") == (
        "compose.yaml:compose.traefik.yaml:compose.grafana.yaml:compose.metrics.yaml"
    )
    assert (tmp_path / ".env").read_text().startswith("# the operator's own note\n")


def test_0003_honours_the_operators_path_separator(tmp_path):
    files = "compose.yaml;compose.expose.yaml;compose.metrics.yaml"
    env, _, _ = run_0003(tmp_path, f"COMPOSE_PATH_SEPARATOR=;\nCOMPOSE_FILE={files}\n")
    assert env.get("COMPOSE_FILE").split(";") == [
        "compose.yaml",
        "compose.expose.yaml",
        "compose.grafana.yaml",
        "compose.metrics.yaml",
    ]


def test_0003_leaves_a_file_list_without_metrics_alone(tmp_path):
    env, _, _ = run_0003(tmp_path, "COMPOSE_FILE=compose.yaml:compose.expose.yaml\n")
    assert env.get("COMPOSE_FILE") == "compose.yaml:compose.expose.yaml"


def test_0003_creates_the_log_store_directory(tmp_path):
    _, paths, _ = run_0003(tmp_path, "")
    assert (paths.data / "logs" / "victoria").is_dir()


def test_0003_on_a_bare_env_pins_the_database_task_log(tmp_path):
    """An install that never chose a logger class used DbLogger; it keeps it explicitly."""
    env, _, _ = run_0003(tmp_path, "COMPOSE_FILE=compose.yaml:compose.expose.yaml\n")
    assert env.get("NEOPS_LOG_BACKEND") == "db"
    assert not env.is_set("NEOPS_LOG_RETENTION") and not env.is_set("NEOPS_LOG_CLASS")


def test_0003_maps_the_db_logger_class_to_the_db_backend(tmp_path):
    env, _, _ = run_0003(tmp_path, "NEOPS_LOGGER_CLASS=neops.core.log.loggers.db.DbLogger\n")
    assert env.get("NEOPS_LOG_BACKEND") == "db" and not env.is_set("NEOPS_LOG_CLASS")
    assert "NEOPS_LOGGER_CLASS" not in (tmp_path / ".env").read_text()


def test_0003_keeps_the_optimized_db_logger_as_the_explicit_class(tmp_path):
    optimized = "neops.core.log.loggers.optimized_db.OptimizedDbLogger"
    env, _, _ = run_0003(tmp_path, f"NEOPS_LOGGER_CLASS={optimized}\n")
    assert env.get("NEOPS_LOG_BACKEND") == "db" and env.get("NEOPS_LOG_CLASS") == optimized
    assert "NEOPS_LOGGER_CLASS" not in (tmp_path / ".env").read_text()


def test_0003_falls_back_to_the_db_backend_for_any_other_class_and_says_so(tmp_path):
    """Compose never passed a remote store's URL to the CMS, so no other class can have worked."""
    env, _, logs = run_0003(tmp_path, "NEOPS_LOGGER_CLASS=neops.core.log.loggers.loki.LokiLogger\n")
    assert env.get("NEOPS_LOG_BACKEND") == "db" and not env.is_set("NEOPS_LOG_CLASS")
    assert any("LokiLogger" in line and "NEOPS_LOG_BACKEND=db" in line for line in logs)


@pytest.mark.parametrize(
    "old,enabled,horizon",
    [
        ("EXECUTION_LOG_RETENTION_ENABLED=True\nEXECUTION_LOG_RETENTION_DAYS=14\n", "True", "14d"),
        ("EXECUTION_LOG_RETENTION_DAYS=120\n", "", "120d"),
        ("EXECUTION_LOG_RETENTION_ENABLED=True\n", "True", "90d"),
        ("EXECUTION_LOG_RETENTION_ENABLED=False\n", "False", ""),
    ],
    ids=["days-set", "days-set-retention-off", "enabled-without-days", "disabled-without-days"],
)
def test_0003_carries_the_retention_horizon_over_unchanged(tmp_path, old, enabled, horizon):
    env, _, _ = run_0003(tmp_path, old)
    assert env.get("NEOPS_LOG_RETENTION_ENABLED") == enabled
    assert env.get("NEOPS_LOG_RETENTION") == horizon
    assert "EXECUTION_LOG_RETENTION" not in (tmp_path / ".env").read_text()


def test_0003_never_lets_an_enabled_retention_fall_to_the_new_30_day_default(tmp_path):
    """The old default was 90 days: 30d here would delete two months of executions on the
    first start after the upgrade."""
    env, _, _ = run_0003(tmp_path, "COMPOSE_FILE=compose.yaml\nEXECUTION_LOG_RETENTION_ENABLED=True\n")
    assert env.get("NEOPS_LOG_RETENTION") == "90d"


@pytest.mark.parametrize(
    "toggle,horizon",
    [
        ("EXECUTION_LOG_RETENTION_ENABLED=y", "90d"),
        ("EXECUTION_LOG_RETENTION_ENABLED=OK", "90d"),
        ("EXECUTION_LOG_RETENTION_ENABLED=2", "90d"),
        ("EXECUTION_LOG_RETENTION_ENABLED=-1", "90d"),
        ("NEOPS_LOG_RETENTION_ENABLED=ok", "90d"),
        ("EXECUTION_LOG_RETENTION_ENABLED=0", ""),
        ("EXECUTION_LOG_RETENTION_ENABLED=n", ""),
        ("EXECUTION_LOG_RETENTION_ENABLED=off", ""),
    ],
)
def test_0003_counts_the_retention_toggle_as_on_exactly_when_the_cms_does(tmp_path, toggle, horizon):
    """The CMS reads the toggle with django-environ's env.bool, where y, ok and any non-zero
    integer are on too; each of those must keep the 90-day horizon."""
    env, _, _ = run_0003(tmp_path, f"{toggle}\n")
    assert env.get("NEOPS_LOG_RETENTION") == horizon


@pytest.mark.parametrize(
    "text,enabled,horizon",
    [
        ("EXECUTION_LOG_RETENTION_ENABLED=True\nEXECUTION_LOG_RETENTION_ENABLED=False\n", "False", ""),
        ("EXECUTION_LOG_RETENTION_ENABLED=False\nEXECUTION_LOG_RETENTION_ENABLED=True\n", "True", "90d"),
        ("EXECUTION_LOG_RETENTION_ENABLED = y\n", "y", "90d"),
    ],
    ids=["duplicate-last-off", "duplicate-last-on", "spaced"],
)
def test_0003_reads_the_retention_toggle_the_way_compose_does(tmp_path, text, enabled, horizon):
    """Compose takes the last of a duplicated key and allows spaces around `=`."""
    env, _, _ = run_0003(tmp_path, text)
    assert env.get("NEOPS_LOG_RETENTION_ENABLED") == enabled
    assert env.get("NEOPS_LOG_RETENTION") == horizon
    assert "EXECUTION_LOG_RETENTION" not in (tmp_path / ".env").read_text()


def test_0003_keeps_the_logger_class_compose_reads_from_a_duplicated_key(tmp_path):
    loggers = "neops.core.log.loggers"
    text = (
        f"NEOPS_LOGGER_CLASS={loggers}.db.DbLogger\n"
        f"NEOPS_LOGGER_CLASS={loggers}.optimized_db.OptimizedDbLogger\n"
    )
    env, _, _ = run_0003(tmp_path, text)
    assert env.get("NEOPS_LOG_CLASS") == f"{loggers}.optimized_db.OptimizedDbLogger"
    assert "NEOPS_LOGGER_CLASS" not in (tmp_path / ".env").read_text()


def test_0003_keeps_a_retention_the_operator_already_set_in_the_new_name(tmp_path):
    text = "NEOPS_LOG_RETENTION_ENABLED=True\nNEOPS_LOG_RETENTION=2w\nEXECUTION_LOG_RETENTION_DAYS=90\n"
    env, _, _ = run_0003(tmp_path, text)
    assert env.get("NEOPS_LOG_RETENTION") == "2w"
    assert "EXECUTION_LOG_RETENTION_DAYS" not in (tmp_path / ".env").read_text()


def test_0003_is_a_no_op_when_run_again(tmp_path):
    text = METRICS_DOTENV + (
        "NEOPS_LOGGER_CLASS=neops.core.log.loggers.optimized_db.OptimizedDbLogger\n"
        "EXECUTION_LOG_RETENTION_ENABLED=True\n"
    )
    env, paths, _ = run_0003(tmp_path, text)
    once = (tmp_path / ".env").read_text()
    module = migrate.discover(paths.migrations)[0]
    module.apply(migrate.Ctx(tmp_path, paths.data, env, log=lambda m: None))
    assert (tmp_path / ".env").read_text() == once
