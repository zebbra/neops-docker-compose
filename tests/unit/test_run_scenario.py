"""The e2e harness's clone handling is checked here, where no Docker is needed.

A rerun with --keep or an existing --workdir reuses the clone, and it must test the commit the
checkout is on now, not the one it was cloned at.
"""

import subprocess
import sys
from pathlib import Path

import pytest

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
