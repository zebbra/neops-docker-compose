import os
import shutil
import subprocess

import pytest

from neops_compose import ownership

NOBODY_UID = 65534


def docker_can_run() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "version"], capture_output=True).returncode == 0


needs_docker = pytest.mark.skipif(not docker_can_run(), reason="no reachable docker daemon")


@needs_docker
def test_chown_via_container_changes_and_restores_ownership_without_sudo(tmp_path):
    """The point of the container is doing what the operator cannot: hand a tree to a uid they
    are not, and take it back afterwards. Both directions are exercised, or purge stays broken
    on exactly the trees Postgres and Elasticsearch leave behind."""
    target = tmp_path / "data"
    (target / "postgres").mkdir(parents=True)
    (target / "postgres" / "PG_VERSION").write_text("16\n")

    ownership.chown_via_container(target, NOBODY_UID, NOBODY_UID)
    assert (target / "postgres" / "PG_VERSION").stat().st_uid == NOBODY_UID

    ownership.chown_via_container(target, os.getuid(), os.getgid())
    assert (target / "postgres" / "PG_VERSION").stat().st_uid == os.getuid()
    assert (target / "postgres" / "PG_VERSION").read_text() == "16\n"


def test_a_failing_chown_names_the_path_and_the_docker_error(tmp_path, monkeypatch):
    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 125, "", "docker: permission denied\n")

    monkeypatch.setattr(ownership.subprocess, "run", failed)
    with pytest.raises(ownership.OwnershipError) as exc:
        ownership.chown_via_container(tmp_path, 472, 472)
    message = str(exc.value)
    assert str(tmp_path) in message and "472:472" in message
    assert "permission denied" in message
