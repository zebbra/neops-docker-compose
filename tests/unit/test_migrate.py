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
