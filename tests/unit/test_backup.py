import json
import stat

from neops_compose import backup
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


def test_backup_archive_is_self_contained_and_private(tmp_path):
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
    target = backup.create(env, Scenario.from_env(env), paths, FakeCompose(), state, log=lambda m: None)
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
