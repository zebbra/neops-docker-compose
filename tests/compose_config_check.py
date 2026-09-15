#!/usr/bin/env python3
"""For every examples/*.env: fill dummy secrets, render generated/, run `docker compose config`.

A cheap gate that catches a broken overlay merge or a missing interpolation before any container starts.
Run from the repo root: uv run python tests/compose_config_check.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SECRET_KEYS = (
    "NEOPS_CMS_DB_PASSWORD",
    "NEOPS_ENGINE_DB_PASSWORD",
    "DJANGO_SECRET_KEY",
    "NEOPS_ADMIN_PASSWORD",
    "NEOPS_KEYCLOAK_ADMIN_PASSWORD",
    "NEOPS_KEYCLOAK_DB_PASSWORD",
    "NEOPS_GRAFANA_ADMIN_PASSWORD",
    "NEOPS_OIDC_CLIENT_ID",
    "NEOPS_OIDC_CLIENT_SECRET",
)


def main() -> int:
    sys.path.insert(0, str(REPO))
    from neops_compose import secrets
    from neops_compose.env import Env
    from neops_compose.paths import Paths
    from neops_compose.render import render
    from neops_compose.scenario import Scenario

    failures = 0
    for example in sorted((REPO / "examples").glob("*.env")):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "repo"
            scratch.mkdir()
            for f in list(REPO.glob("compose*.yaml")) + [REPO / "metrics"]:
                (scratch / f.name).symlink_to(f)
            (scratch / "certs").mkdir()
            (scratch / "cust-cert").mkdir()
            for name in ("cert.pem", "key.pem"):
                (scratch / "certs" / name).write_text("placeholder")
            text = example.read_text()
            for key in SECRET_KEYS:
                text = text.replace(f"\n{key}=\n", f"\n{key}=dummy0123456789abcdef\n")
            (scratch / ".env").write_text(text)
            env = Env(scratch / ".env")
            paths = Paths.for_repo(scratch, env)
            for d in paths.data_dirs():
                d.mkdir(parents=True, exist_ok=True)
            secrets.ensure_jwt(paths.jwt_dir)
            secrets.ensure_keycloak_client_secret(paths.keycloak_client_env)
            if env.flag("NEOPS_TLS_SELF_SIGNED"):
                secrets.ensure_selfsigned(paths.tls_dir, ["neops.example.com"])
            paths.engine_env.write_text("NEOPS_CMS_TOKEN=dummy\n")
            render(env, Scenario.from_env(env), paths)
            result = subprocess.run(
                ["docker", "compose", "config", "--quiet"], cwd=scratch, capture_output=True, text=True
            )
            status = "ok  " if result.returncode == 0 else "FAIL"
            print(f"{status} {example.name}")
            if result.returncode != 0:
                failures += 1
                print(result.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
