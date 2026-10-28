"""Rebuilding the CMS's Elasticsearch indices once they no longer match neops-core or Elasticsearch.

A neops-core with `elastic_index --rebuild-if-stale` decides that itself: it stamps every index
with the definition hash and Elasticsearch version it was built for, and rebuilds exactly what is
stale. An older core cannot say whether its indices changed, so compose rebuilds everything
whenever the core or the Elasticsearch image moved.
"""

from __future__ import annotations

import json

from neops_compose.compose import ComposeError
from neops_compose.context import Ctx

# -u: Python block-buffers a pipe, and the core image does not set PYTHONUNBUFFERED, so without it
# a rebuild prints nothing until it exits.
MANAGE = ("python", "-u", "manage.py", "elastic_index")
REBUILD_OWNERS = ("cms", "elasticsearch")
DO_NOT_INTERRUPT = (
    "this can take minutes. Do not interrupt it: an interrupted rebuild keeps running inside cms"
)
SEE_DOCS = "(docs/30-operations.md#upgrades)"
# --rebuild-if-stale finished the rebuild but set aside documents Elasticsearch refused.
SET_ASIDE = 3
INDEX_SYNC_REPORT = ("python", "manage.py", "index_sync_status", "--json")
INDEX_SYNC_REPORT_VERSION = 1
LIST_REJECTED = "./neops compose -- exec cms python manage.py index_sync_status --list rejected"


def refresh(ctx: Ctx, last_up: dict | None, running: dict[str, str]) -> None:
    """Runs against the healthy `cms`, not in cms-init: `cms` waits for cms-init, so a rebuild
    there would take the whole CMS API down, not only search."""
    usage = ctx.compose.exec("cms", *MANAGE, "--help")
    if "--rebuild-if-stale" in usage:
        ctx.log(
            f"neops-core checks the search index and rebuilds what is stale; {DO_NOT_INTERRUPT}, "
            f"and the next ./neops up waits for it at cms-init {SEE_DOCS}"
        )
        if _stream(ctx, "--rebuild-if-stale", tolerated=(SET_ASIDE,)) == SET_ASIDE:
            ctx.log(_set_aside_warning(ctx))
    elif images_moved(last_up, running):
        ctx.log(
            f"neops-core or Elasticsearch changed: rebuilding the search index; {DO_NOT_INTERRUPT}, "
            "and the next ./neops up would start a second one beside it: let it finish, or stop it "
            f"with ./neops restart cms, before running ./neops up again {SEE_DOCS}"
        )
        lookups = ["--sync-lookups"] if "--sync-lookups" in usage else []
        _stream(ctx, "--rebuild", "--force", *lookups)


def images_moved(last_up: dict | None, running: dict[str, str]) -> bool:
    """A fresh install has nothing to rebuild: cms-init has just created the indices empty."""
    if not last_up:
        return False
    before = last_up.get("images", {})
    return any(before.get(service) != running.get(service) for service in REBUILD_OWNERS)


def _stream(ctx: Ctx, *flags: str, tolerated: tuple[int, ...] = ()) -> int:
    """Not captured: a rebuild runs for minutes, and a failed populate prints its traceback."""
    code = ctx.compose.run("exec", "-T", "cms", *MANAGE, *flags, check=False).returncode
    if code != 0 and code not in tolerated:
        raise ComposeError(
            f"the search index rebuild failed (exit {code}, its output is above) and this start was not "
            "recorded; fix the cause and run ./neops up again"
        )
    return code


def _set_aside_warning(ctx: Ctx) -> str:
    count = _rejected_rows(ctx)
    documents = "some documents were" if count is None else f"{count} documents were"
    return (
        f"WARNING: the search index was rebuilt, but {documents} set aside: Elasticsearch refused them, "
        f"and they stay out of search until their rows change. See {LIST_REJECTED}"
    )


def _rejected_rows(ctx: Ctx) -> int | None:
    """Only the number is at stake here, so an answer this release cannot read just leaves it out.
    index_sync_status exits 1 while rows are rejected, after it has printed its report."""
    try:
        result = ctx.compose.run("exec", "-T", "cms", *INDEX_SYNC_REPORT, capture=True, check=False)
    except ComposeError:
        return None
    report = _last_json_line(result.stdout or "")
    if report.get("version") != INDEX_SYNC_REPORT_VERSION:
        return None
    try:
        return sum(int(model["rejected"]) for model in report["models"].values())
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _last_json_line(stdout: str) -> dict:
    """Core prints its report as the last line; Django may print startup lines before it."""
    lines = [line for line in stdout.splitlines() if line.startswith("{")]
    try:
        report = json.loads(lines[-1])
    except (IndexError, ValueError):
        return {}
    return report if isinstance(report, dict) else {}
