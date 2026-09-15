import json
import os
import stat

from neops_compose import backup
from neops_compose.context import Ctx
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.state import State


class FakeCompose:
    def running_services(self):
        return {"postgres-cms", "postgres-engine", "cms"}

    def exec_bytes(self, service, *cmd, env=None):
        return f"DUMP {service} {' '.join(cmd)}".encode()

    def images(self):
        return ["quay.io/zebbra/neops-core:2.1.0-beta.5"]


def make_ctx(tmp_path) -> Ctx:
    (tmp_path / ".env").write_text(
        "COMPOSE_FILE=compose.yaml:compose.expose.yaml\n"
        "NEOPS_CMS_DB_PASSWORD=pw\n"
        "NEOPS_ENGINE_DB_PASSWORD=pw2\n"
    )
    env = Env(tmp_path / ".env")
    paths = Paths.for_repo(tmp_path, env)
    paths.secrets.mkdir(parents=True)
    (paths.secrets / "engine.env").write_text("NEOPS_CMS_TOKEN=t\n")
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "cert.pem").write_text("cert")
    state = State()
    state.record_applied("0001_initial_layout")
    state.save(paths.state_file)
    return Ctx(
        repo=tmp_path,
        env=env,
        paths=paths,
        scenario=Scenario.from_env(env),
        compose=FakeCompose(),
        state=state,
        log=lambda m: None,
    )


def test_backup_archive_is_self_contained_and_private(tmp_path):
    target = backup.create(make_ctx(tmp_path))
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert (target / "cms.dump").read_bytes() == b"DUMP postgres-cms pg_dump -U neops -Fc neops"
    assert (
        target / "engine.dump"
    ).read_bytes() == b"DUMP postgres-engine pg_dump -U postgres -Fc neops-workflow"
    assert not (target / "keycloak.dump").exists()
    assert (target / ".env").exists()
    assert (target / "secrets" / "engine.env").exists()
    assert (target / "certs" / "cert.pem").exists()
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["images"] == ["quay.io/zebbra/neops-core:2.1.0-beta.5"]
    assert manifest["applied"] == ["0001_initial_layout"]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in target.rglob("*") if p.is_file())


def test_the_archive_is_private_before_the_dumps_land_in_it(tmp_path, monkeypatch):
    seen = []
    real = backup._dump_databases

    def spy(compose, target, log):
        seen.append(stat.S_IMODE(target.stat().st_mode))
        return real(compose, target, log)

    monkeypatch.setattr(backup, "_dump_databases", spy)
    backup.create(make_ctx(tmp_path))
    assert seen == [0o700]


def test_an_operator_supplied_dir_keeps_its_own_mode(tmp_path):
    ctx = make_ctx(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.chmod(elsewhere, 0o755)
    target = backup.create(ctx, target_root=elsewhere)
    assert target.parent == elsewhere
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_two_backups_in_the_same_second_do_not_raise(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr(backup, "_stamp", lambda: "20260914T120000Z")
    first = backup.create(ctx)
    second = backup.create(ctx)
    assert first == second and (second / "manifest.json").exists()


def test_prune_keeps_newest_n(tmp_path):
    b = tmp_path / "backups"
    for name in (
        "20260101T000000Z",
        "20260102T000000Z",
        "20260103T000000Z",
        "pre-migrate-20260104T000000Z",
    ):
        (b / name).mkdir(parents=True)
    removed = backup.prune(b, keep=2)
    assert removed == [b / "20260101T000000Z"]
    assert (b / "pre-migrate-20260104T000000Z").exists()


def test_prune_never_touches_a_directory_it_did_not_create(tmp_path):
    shared = tmp_path / "archive"
    strangers = ("2026-project-data", "2026", "20260101T000000Z.old", "notes")
    for name in ("20260101T000000Z", "20260102T000000Z", *strangers):
        (shared / name).mkdir(parents=True)
    removed = backup.prune(shared, keep=1)
    assert removed == [shared / "20260101T000000Z"]
    assert all((shared / name).exists() for name in strangers)
