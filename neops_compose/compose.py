from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

WAIT_TIMEOUT = 900


class ComposeError(RuntimeError):
    pass


class Compose:
    """docker compose, always run from the repo root so COMPOSE_FILE's relative entries resolve."""

    def __init__(self, repo: Path):
        self.repo = repo

    def run(
        self, *args: str, capture: bool = False, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["docker", "compose", *args],
            cwd=self.repo,
            text=True,
            capture_output=capture,
            env=(os.environ | env) if env else None,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or "").strip() if capture else ""
            raise ComposeError(
                f"docker compose {' '.join(args)} failed ({result.returncode}) {detail}".strip()
            )
        return result

    def up(self, *services: str, wait: bool = True, force_recreate: bool = False) -> None:
        args = ["up", "-d"]
        if wait:
            args += ["--wait", "--wait-timeout", str(WAIT_TIMEOUT)]
        if force_recreate:
            args.append("--force-recreate")
        self.run(*args, *services)

    def pull(self) -> None:
        self.run("pull", "--quiet")

    def down(self) -> None:
        self.run("down", "--remove-orphans")

    def exec(self, service: str, *cmd: str, env: dict[str, str] | None = None) -> str:
        """`-e KEY` carries no value: compose reads it from our environment, keeping it out of argv."""
        flags: list[str] = []
        for key in env or {}:
            flags += ["-e", key]
        return self.run("exec", "-T", *flags, service, *cmd, capture=True, env=env).stdout

    def exec_bytes(self, service: str, *cmd: str, env: dict[str, str] | None = None) -> bytes:
        """Binary stdout (pg_dump -Fc); `-e KEY` keeps values out of argv, exactly as exec() does."""
        flags: list[str] = []
        for key in env or {}:
            flags += ["-e", key]
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", *flags, service, *cmd],
            cwd=self.repo,
            capture_output=True,
            env=(os.environ | env) if env else None,
        )
        if result.returncode != 0:
            raise ComposeError(
                f"docker compose exec {service} {cmd[0]} failed: "
                f"{result.stderr.decode(errors='replace').strip()}"
            )
        return result.stdout

    def ps(self) -> list[dict]:
        out = self.run("ps", "-a", "--format", "json", capture=True).stdout.strip()
        if not out:
            return []
        if out.startswith("["):
            return json.loads(out)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def running_services(self) -> set[str]:
        return {row["Service"] for row in self.ps() if row.get("State") == "running"}

    def images(self) -> list[str]:
        out = self.run("config", "--images", capture=True).stdout
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    def service_names(self) -> list[str]:
        out = self.run("config", "--services", capture=True).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
