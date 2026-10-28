"""Rebuilding the CMS's Elasticsearch indices once they no longer match neops-core or Elasticsearch.

A neops-core with `elastic_index --rebuild-if-stale` decides that itself: it stamps every index
with the definition hash and Elasticsearch version it was built for, and rebuilds exactly what is
stale. An older core cannot say whether its indices changed, so compose rebuilds everything
whenever the core or the Elasticsearch image moved.
"""

from __future__ import annotations

from neops_compose.compose import ComposeError
from neops_compose.context import Ctx

# -u: Python block-buffers a pipe, and the core image does not set PYTHONUNBUFFERED, so without it
# a rebuild prints nothing until it exits.
MANAGE = ("python", "-u", "manage.py", "elastic_index")
REBUILD_OWNERS = ("cms", "elasticsearch")
DO_NOT_INTERRUPT = (
    "this can take minutes. Do not interrupt it: an interrupted rebuild keeps running inside cms, "
    "and the next ./neops up waits for it at cms-init (docs/30-operations.md#upgrades)"
)


def refresh(ctx: Ctx, last_up: dict | None, running: dict[str, str]) -> None:
    """Runs against the healthy `cms`, not in cms-init: `cms` waits for cms-init, so a rebuild
    there would take the whole CMS API down, not only search."""
    usage = ctx.compose.exec("cms", *MANAGE, "--help")
    if "--rebuild-if-stale" in usage:
        ctx.log(f"neops-core checks the search index and rebuilds what is stale; {DO_NOT_INTERRUPT}")
        _stream(ctx, "--rebuild-if-stale")
    elif images_moved(last_up, running):
        ctx.log(f"neops-core or Elasticsearch changed: rebuilding the search index; {DO_NOT_INTERRUPT}")
        lookups = ["--sync-lookups"] if "--sync-lookups" in usage else []
        _stream(ctx, "--rebuild", "--force", *lookups)


def images_moved(last_up: dict | None, running: dict[str, str]) -> bool:
    """A fresh install has nothing to rebuild: cms-init has just created the indices empty."""
    if not last_up:
        return False
    before = last_up.get("images", {})
    return any(before.get(service) != running.get(service) for service in REBUILD_OWNERS)


def _stream(ctx: Ctx, *flags: str) -> None:
    """Not captured: a rebuild runs for minutes, and a failed populate prints its traceback."""
    try:
        ctx.compose.run("exec", "-T", "cms", *MANAGE, *flags)
    except ComposeError as exc:
        raise ComposeError(
            f"the search index rebuild failed (its output is above) and this start was not recorded; "
            f"fix the cause and run ./neops up again. {exc}"
        ) from exc
