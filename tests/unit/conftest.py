from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo() -> Path:
    return REPO


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A throwaway repo root with the real compose files and templates symlinked in."""
    (tmp_path / "migrations").symlink_to(REPO / "migrations")
    for f in REPO.glob("compose*.yaml"):
        (tmp_path / f.name).symlink_to(f)
    (tmp_path / "certs").mkdir()
    (tmp_path / "cust-cert").mkdir()
    return tmp_path
