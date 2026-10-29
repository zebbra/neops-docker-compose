"""When the CMS's Elasticsearch indices must be rebuilt, and rebuilding them.

neops-core prints a hash of every index it manages (`elastic_index --schema-hash`), so a core
upgrade costs a rebuild only when it changes the indices. Elasticsearch upgrades always cost
one. A core without the flag reports no schema and is never the reason for a rebuild, except on
the first start of a core that has one, which no recorded hash can vouch for.
"""

from __future__ import annotations

import re

from neops_compose.compose import ComposeError
from neops_compose.context import Ctx

MANAGE = ("python", "manage.py", "elastic_index")
SCHEMA_HASH = re.compile(r"[0-9a-f]{64}")


def read_schema(ctx: Ctx) -> str | None:
    """None only for a core that rejects the flag. Any other failure raises: recording a start
    without the hash would leave a schema change undetected for good."""
    args = ("exec", "-T", "cms", *MANAGE, "--schema-hash")
    result = ctx.compose.run(*args, capture=True, check=False)
    if result.returncode != 0:
        if "unrecognized arguments" in (result.stderr or ""):
            return None
        raise ComposeError(
            f"elastic_index --schema-hash failed ({result.returncode}) {result.stderr.strip()}"
        )
    return _last_line_hash(result.stdout)


def _last_line_hash(stdout: str) -> str:
    """manage.py prints plugin noise to stdout before the hash, which is always the last line."""
    lines = stdout.strip().splitlines()
    last = lines[-1].strip() if lines else ""
    if not SCHEMA_HASH.fullmatch(last):
        raise ComposeError(f"elastic_index --schema-hash printed no hash; last line: {last!r}")
    return last


def refresh(ctx: Ctx, last_up: dict | None, running: dict[str, str]) -> str | None:
    """Rebuilds a stale index and returns the schema the start is to be recorded with."""
    schema = read_schema(ctx)
    if is_stale(last_up, running, schema):
        rebuild(ctx)
    return schema


def is_stale(last_up: dict | None, running: dict[str, str], schema: str | None) -> bool:
    """A fresh install has nothing to rebuild: cms-init has just created the indices empty."""
    if not last_up:
        return False
    before = last_up.get("images", {})
    if before.get("elasticsearch") != running.get("elasticsearch"):
        return True
    if schema is None:
        return False
    recorded = last_up.get("search_schema")
    if recorded is None:
        return before.get("cms") != running.get("cms")
    return recorded != schema


def rebuild(ctx: Ctx) -> None:
    """Streamed, not captured: it runs for minutes, and a failed populate prints its traceback."""
    flags = ["--rebuild", "--force"]
    if supports_sync_lookups(ctx):
        flags.append("--sync-lookups")
    ctx.log("the search index no longer matches neops-core or Elasticsearch: rebuilding it")
    ctx.compose.run("exec", "-T", "cms", *MANAGE, *flags)


def supports_sync_lookups(ctx: Ctx) -> bool:
    """Only a neops-core with ES|QL lookup indices has the flag."""
    return "--sync-lookups" in ctx.compose.exec("cms", *MANAGE, "--help")
