"""Rebuilding the CMS's Elasticsearch indices once they no longer match neops-core or Elasticsearch.

A neops-core with `elastic_index --rebuild-if-stale` decides that itself: it stamps every index
with the definition hash and Elasticsearch version it was built for, and rebuilds exactly the
stale ones. For an older core, compose rebuilds everything whenever the Elasticsearch image moved.
"""

from __future__ import annotations

from neops_compose.context import Ctx

MANAGE = ("python", "manage.py", "elastic_index")


def refresh(ctx: Ctx, last_up: dict | None, running: dict[str, str]) -> None:
    """Runs against the healthy `cms`, not in cms-init: `cms` waits for cms-init, so a rebuild
    there would take the whole CMS API down, not only search."""
    usage = ctx.compose.exec("cms", *MANAGE, "--help")
    if "--rebuild-if-stale" in usage:
        ctx.log("checking whether the search index matches neops-core and Elasticsearch")
        _stream(ctx, "--rebuild-if-stale")
    elif elasticsearch_moved(last_up, running):
        ctx.log("Elasticsearch changed since the last start: rebuilding the search index")
        lookups = ["--sync-lookups"] if "--sync-lookups" in usage else []
        _stream(ctx, "--rebuild", "--force", *lookups)


def elasticsearch_moved(last_up: dict | None, running: dict[str, str]) -> bool:
    """A fresh install has nothing to rebuild: cms-init has just created the indices empty."""
    if not last_up:
        return False
    return last_up.get("images", {}).get("elasticsearch") != running.get("elasticsearch")


def _stream(ctx: Ctx, *flags: str) -> None:
    """Not captured: a rebuild runs for minutes, and a failed populate prints its traceback."""
    ctx.compose.run("exec", "-T", "cms", *MANAGE, *flags)
