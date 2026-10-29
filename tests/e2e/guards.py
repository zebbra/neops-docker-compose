"""Refusals shared by every harness entry point that runs Docker Compose."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

# Docker Compose prefers these from the shell over .env, and Compose.run passes the shell on.
SHELL_COMPOSE_KEYS = ("COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "COMPOSE_ENV_FILES")


def shell_compose_refusal(environ: Mapping[str, str], clone: Path) -> str | None:
    """Why a compose run from this shell would not act on `clone` alone, or None."""
    inherited = [key for key in SHELL_COMPOSE_KEYS if key in environ]
    if not inherited:
        return None
    return f"refusing: {', '.join(inherited)} is set in the shell and would win over {clone}/.env"
