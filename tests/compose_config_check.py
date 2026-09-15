#!/usr/bin/env python3
"""For every examples/*.env: fill dummy secrets, render generated/, run `docker compose config`.

A cheap gate that catches a broken overlay merge or a missing interpolation before any container starts.
Run from the repo root: uv run python tests/compose_config_check.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DUMMY_SECRET = "dummy0123456789abcdef"


def _fill_secrets(text: str, keys: tuple[str, ...]) -> str:
    """Replace a still-blank `KEY=` line with a dummy value, for every key in `keys`.

    Per-line rather than a literal `\\nKEY=\\n` match: that literal form misses a blank key on
    the first line of the file (no leading newline) and a blank key on the last line when the
    file has no trailing newline, both of which are exactly the kind of file an operator hand-edits.
    """
    pattern = re.compile(r"^(" + "|".join(re.escape(k) for k in keys) + r")=$", re.MULTILINE)
    return pattern.sub(lambda m: f"{m.group(1)}={DUMMY_SECRET}", text)


def main() -> int:
    sys.path.insert(0, str(REPO))
    from neops_compose import secrets
    from neops_compose.env import Env
    from neops_compose.paths import Paths
    from neops_compose.render import render
    from neops_compose.rules import ALL_SECRET_KEYS
    from neops_compose.scenario import Scenario

    examples = sorted((REPO / "examples").glob("*.env"))
    if not examples:
        print("no examples/*.env found")
        return 1

    failures = 0
    for example in examples:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "repo"
            scratch.mkdir()
            for f in list(REPO.glob("compose*.yaml")) + [REPO / "metrics"]:
                (scratch / f.name).symlink_to(f)
            (scratch / "certs").mkdir()
            (scratch / "cust-cert").mkdir()
            for name in ("cert.pem", "key.pem"):
                (scratch / "certs" / name).write_text("placeholder")
            text = _fill_secrets(example.read_text(), ALL_SECRET_KEYS)
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
