from __future__ import annotations

import json
import subprocess
from pathlib import Path

WAIT_TIMEOUT = 900


class ComposeError(RuntimeError):
    pass


class Compose:
    """docker compose, always run from the repo root so COMPOSE_FILE's relative entries resolve."""

    def __init__(self, repo: Path):
        self.repo = repo

    def run(self, *args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["docker", "compose", *args], cwd=self.repo, text=True, capture_output=capture
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
        flags: list[str] = []
        for key, value in (env or {}).items():
            flags += ["-e", f"{key}={value}"]
        return self.run("exec", "-T", *flags, service, *cmd, capture=True).stdout

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
