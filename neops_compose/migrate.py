from __future__ import annotations

import datetime as dt
import importlib.util
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.secrets import write_secret
from neops_compose.state import State

NAME_RE = re.compile(r"^\d{4}_[a-z0-9_]+$")
CHOWN_IMAGE = "postgres:16-alpine"


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    name: str
    description: str
    idempotent: bool
    module: ModuleType

    def apply(self, ctx: Ctx) -> None:
        self.module.apply(ctx)


@dataclass
class Ctx:
    """What a migration may touch. Migrations run before containers start."""

    repo: Path
    data: Path
    env: Env
    log: Callable[[str], None]

    def mkdir(self, path: Path, mode: int | None = None) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if mode is not None:
            os.chmod(path, mode)

    def move(self, src: Path, dst: Path) -> None:
        if not src.exists():
            self.log(f"skip move, {src} does not exist")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        self.log(f"moved {src} -> {dst}")

    def chown_via_container(self, path: Path, uid: int, gid: int) -> None:
        """chown without sudo: docker runs as root, so a throwaway container can do it."""
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{path.resolve()}:/target",
                CHOWN_IMAGE,
                "chown",
                "-R",
                f"{uid}:{gid}",
                "/target",
            ],
            check=True,
        )
        self.log(f"chowned {path} to {uid}:{gid}")

    def compose(self, *args: str) -> None:
        subprocess.run(["docker", "compose", *args], cwd=self.repo, check=True)


def discover(migrations_dir: Path) -> list[Migration]:
    out: list[Migration] = []
    for file in sorted(migrations_dir.glob("*.py")):
        if not NAME_RE.match(file.stem):
            raise MigrationError(f"migration file {file.name} must be named NNNN_slug.py")
        spec = importlib.util.spec_from_file_location(f"neops_migration_{file.stem}", file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "apply", None)):
            raise MigrationError(f"migration {file.stem} has no apply(ctx)")
        out.append(
            Migration(
                file.stem,
                getattr(module, "DESCRIPTION", file.stem),
                bool(getattr(module, "IDEMPOTENT", False)),
                module,
            )
        )
    return out


def pending(migrations: list[Migration], state: State) -> list[Migration]:
    done = set(state.applied_names)
    return [m for m in migrations if m.name not in done]


def snapshot(paths: Paths) -> Path:
    """A copy of .env and the state file; both hold secrets, so nothing here is readable by others."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = paths.backups / f"pre-migrate-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.backups, 0o700)
    os.chmod(target, 0o700)
    for src in (paths.env_file, paths.state_file):
        if src.exists():
            write_secret(target / src.name, src.read_bytes())
    return target


def _failed_marker(paths: Paths, name: str) -> Path:
    return paths.state_file.parent / f"{name}.failed"


def apply_all(
    env: Env, paths: Paths, state: State, log: Callable[[str], None], dry_run: bool = False
) -> list[str]:
    todo = pending(discover(paths.migrations), state)
    if not todo:
        return []
    if dry_run:
        for m in todo:
            log(f"would apply {m.name}: {m.description}")
        return [m.name for m in todo]
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    snap = snapshot(paths)
    log(f"snapshot of .env and state in {snap}")
    ctx = Ctx(paths.repo, paths.data, env, log)
    done: list[str] = []
    for m in todo:
        marker = _failed_marker(paths, m.name)
        if marker.exists() and not m.idempotent:
            raise MigrationError(
                f"{m.name} failed earlier and is not idempotent; repair by hand using the "
                f"snapshot in {marker.read_text().strip() or 'backups/'} then remove {marker}"
            )
        log(f"applying {m.name}: {m.description}")
        try:
            m.apply(ctx)
        except Exception as exc:
            marker.write_text(str(snap) + "\n")
            raise MigrationError(f"{m.name} failed: {exc}") from exc
        if marker.exists():
            marker.unlink()
        state.record_applied(m.name)
        state.save(paths.state_file)
        done.append(m.name)
    return done


def fake(name: str, paths: Paths, state: State) -> None:
    names = [m.name for m in discover(paths.migrations)]
    if name not in names:
        raise MigrationError(f"{name} is not a known migration (known: {', '.join(names)})")
    if name in state.applied_names:
        raise MigrationError(f"{name} is already applied")
    state.record_applied(name)
    state.record_faked(name)
    state.save(paths.state_file)
