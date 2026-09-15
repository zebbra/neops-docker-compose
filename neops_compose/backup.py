from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from neops_compose import __version__
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.state import State

DATABASES = (  # service, user, db, archive name
    ("postgres-cms", "neops", "neops", "cms.dump"),
    ("postgres-engine", "postgres", "neops-workflow", "engine.dump"),
    ("postgres-keycloak", "keycloak", "keycloak", "keycloak.dump"),
)
RESTORE_NOTE = (
    "Elasticsearch is not backed up: after a restore run manage.py elastic_index --create and --populate"
)


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


def create(
    env: Env,
    scenario: Scenario,
    paths: Paths,
    compose,
    state: State,
    log: Callable[[str], None],
    target_root: Path | None = None,
) -> Path:
    """Logical dumps of every running Postgres plus everything needed to rebuild: .env, secrets, certs."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = (target_root or paths.backups) / stamp
    target.mkdir(parents=True)
    dumped = _dump_databases(compose, target, log)
    _private_copy(paths.env_file, target / ".env")
    _private_copy(paths.secrets, target / "secrets")
    _private_copy(paths.certs, target / "certs")
    _private_copy(paths.repo / "cust-cert", target / "cust-cert")
    manifest = {
        "created": stamp,
        "cli": __version__,
        "images": compose.images(),
        "applied": state.applied_names,
        "faked": state.faked_names,
        "dumps": dumped,
        "compose_file": list(scenario.files),
        "note": RESTORE_NOTE,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    _lock_down(target)
    os.chmod(target.parent, 0o700)
    log(f"backup written to {target} (this archive contains every secret of the deployment)")
    return target


def prune(backups_dir: Path, keep: int) -> list[Path]:
    archives = sorted(p for p in backups_dir.iterdir() if p.is_dir() and p.name[:1].isdigit())
    removed = archives[:-keep] if keep > 0 else []
    for p in removed:
        shutil.rmtree(p)
    return removed
