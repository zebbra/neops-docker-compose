from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from neops_compose import __version__
from neops_compose.context import Ctx

DATABASES = (  # service, user, db, archive name
    ("postgres-cms", "neops", "neops", "cms.dump"),
    ("postgres-engine", "postgres", "neops-workflow", "engine.dump"),
    ("postgres-keycloak", "keycloak", "keycloak", "keycloak.dump"),
)
RESTORE_NOTE = (
    "Elasticsearch is not backed up: after a restore run manage.py elastic_index --create and --populate"
)


def _stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _private_copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _lock_down(root: Path) -> None:
    os.chmod(root, 0o700)
    for p in root.rglob("*"):
        os.chmod(p, 0o700 if p.is_dir() else 0o600)


def _dump_databases(compose, target: Path, log: Callable[[str], None]) -> list[str]:
    running = compose.running_services()
    dumped = []
    for service, user, db, name in DATABASES:
        if service not in running:
            continue
        log(f"pg_dump {db} from {service}")
        (target / name).write_bytes(compose.exec_bytes(service, "pg_dump", "-U", user, "-Fc", db))
        dumped.append(name)
    return dumped


def _manifest(ctx: Ctx, stamp: str, dumped: list[str]) -> dict:
    return {
        "created": stamp,
        "cli": __version__,
        "images": ctx.compose.images(),
        "applied": ctx.state.applied_names,
        "faked": ctx.state.faked_names,
        "dumps": dumped,
        "compose_file": list(ctx.scenario.files),
        "note": RESTORE_NOTE,
    }


def create(ctx: Ctx, target_root: Path | None = None) -> Path:
    """Logical dumps of every running Postgres plus everything needed to rebuild: .env, secrets, certs.

    The directory is 0700 from creation, not from _lock_down: the dumps land in it first.
    """
    stamp = _stamp()
    target = (target_root or ctx.paths.backups) / stamp
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    dumped = _dump_databases(ctx.compose, target, ctx.log)
    _private_copy(ctx.paths.env_file, target / ".env")
    _private_copy(ctx.paths.secrets, target / "secrets")
    _private_copy(ctx.paths.certs, target / "certs")
    _private_copy(ctx.paths.cust_certs, target / "cust-cert")
    (target / "manifest.json").write_text(json.dumps(_manifest(ctx, stamp, dumped), indent=2) + "\n")
    _lock_down(target)
    if target_root is None:
        os.chmod(ctx.paths.backups, 0o700)
    ctx.log(f"backup written to {target} (this archive contains every secret of the deployment)")
    return target


def prune(backups_dir: Path, keep: int) -> list[Path]:
    archives = sorted(p for p in backups_dir.iterdir() if p.is_dir() and p.name[:1].isdigit())
    removed = archives[:-keep] if keep > 0 else []
    for p in removed:
        shutil.rmtree(p)
    return removed
