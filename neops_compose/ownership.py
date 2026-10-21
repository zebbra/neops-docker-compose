from __future__ import annotations

import subprocess
from pathlib import Path

CHOWN_IMAGE = "postgres:16-alpine"


class OwnershipError(RuntimeError):
    pass


def chown_via_container(path: Path, uid: int, gid: int) -> None:
    """chown without sudo: docker runs as root, so a throwaway container can do it.

    Every container in the stack writes into data/ as its own uid — Postgres as 70 with
    mode 0700, Grafana as 472, Elasticsearch as 1000 — so the operator can neither read
    nor delete those trees without this.
    """
    result = subprocess.run(
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
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OwnershipError(f"could not chown {path} to {uid}:{gid}: {result.stderr.strip()}")
