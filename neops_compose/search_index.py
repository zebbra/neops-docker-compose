"""Rebuilding the CMS's Elasticsearch indices once they no longer match neops-core or Elasticsearch.

A neops-core with `elastic_index --rebuild-if-stale` decides that itself: it stamps every index
with the definition hash and Elasticsearch version it was built for, and rebuilds exactly what is
stale. An older core cannot say whether its indices changed, so compose rebuilds everything
whenever the core or the Elasticsearch image moved.
"""

from __future__ import annotations

import json

from neops_compose.compose import Compose, ComposeError
from neops_compose.context import Ctx

# -u: Python block-buffers a pipe, and the core image does not set PYTHONUNBUFFERED, so without it
# a rebuild prints nothing until it exits.
MANAGE = ("python", "-u", "manage.py", "elastic_index")
REBUILD_IF_STALE = "--rebuild-if-stale"
REBUILD_OWNERS = ("cms", "elasticsearch")
DO_NOT_INTERRUPT = (
    "this can take minutes. Do not interrupt it: an interrupted rebuild keeps running inside cms"
)
SEE_DOCS = "(docs/30-operations.md#upgrades)"
LEFT_RUNNING = "a search index rebuild from an earlier, interrupted ./neops up is still running in cms"
ABORT_IT = f"To abort it: ./neops restart cms {SEE_DOCS}"
# --rebuild-if-stale finished the rebuild but set aside documents Elasticsearch refused.
SET_ASIDE = 3
INDEX_SYNC_REPORT = ("python", "manage.py", "index_sync_status", "--json")
INDEX_SYNC_REPORT_VERSION = 1
LIST_REJECTED = "./neops compose -- exec -T cms python manage.py index_sync_status --list rejected --limit 0"
LIST_SET_ASIDE = f"list the rows set aside with: {LIST_REJECTED}"
SET_ASIDE_WARNING = (
    "WARNING: the search index was rebuilt, but Elasticsearch refused some documents and neops-core set "
    "them aside as rejected. They stay out of search until their data is corrected and the row saved, or "
    "a later rebuild finds Elasticsearch accepting them. The rebuild's output above gives each reason; "
    f"keep it {SEE_DOCS}"
)


def refresh(ctx: Ctx, last_up: dict | None, running: dict[str, str]) -> None:
    """Runs against the healthy `cms`, not in cms-init: `cms` waits for cms-init, so a rebuild
    there would take the whole CMS API down, not only search."""
    usage = ctx.compose.exec("cms", *MANAGE, "--help")
    if REBUILD_IF_STALE in usage:
        ctx.log(
            f"neops-core checks the search index and rebuilds what is stale; {DO_NOT_INTERRUPT}, "
            f"and the next ./neops up waits for it at cms-init {SEE_DOCS}"
        )
        if _stream(ctx, REBUILD_IF_STALE, tolerated=(SET_ASIDE,)) == SET_ASIDE:
            _report_set_aside(ctx)
    elif images_moved(last_up, running):
        ctx.log(
            f"neops-core or Elasticsearch changed: rebuilding the search index; {DO_NOT_INTERRUPT}, "
            "and the next ./neops up would start a second one beside it: let it finish, or stop it "
            f"with ./neops restart cms, before running ./neops up again {SEE_DOCS}"
        )
        lookups = ["--sync-lookups"] if "--sync-lookups" in usage else []
        _stream(ctx, "--rebuild", "--force", *lookups)


def warn_if_rebuild_running(ctx: Ctx) -> None:
    """Otherwise the next up waits on core's rebuild lock at cms-init with nothing on the screen."""
    processes = _cms_processes(ctx.compose)
    if MANAGE[-1] not in processes:
        return
    if REBUILD_IF_STALE in processes:
        ctx.log(f"{LEFT_RUNNING}; up will wait for it. {ABORT_IT}")
    else:
        ctx.log(f"{LEFT_RUNNING}; nothing waits for it, and up may start a second one beside it. {ABORT_IT}")


def _cms_processes(compose: Compose) -> str:
    """`top`, not `pgrep`, which the core image may lack. Its table differs across compose
    versions, so callers match command names only; an unreadable table must not stop the start."""
    if "cms" not in compose.running_services():
        return ""
    try:
        result = compose.run("top", "cms", capture=True, check=False)
    except ComposeError:
        return ""
    return (result.stdout or "") if result.returncode == 0 else ""


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
            f"elastic_index {' '.join(flags)} exited {code} (its output is above): the search index rebuild "
            "failed and this start was not recorded; fix the cause and run ./neops up again"
        )
    return code


def _report_set_aside(ctx: Ctx) -> None:
    """The warning goes out before the count is asked for, which nothing bounds in time."""
    ctx.log(SET_ASIDE_WARNING)
    ctx.log(_rejected_rows_line(ctx))


def _rejected_rows_line(ctx: Ctx) -> str:
    """index_sync_status exits 1 while rows are rejected, after it has printed its report."""
    try:
        result = ctx.compose.run("exec", "-T", "cms", *INDEX_SYNC_REPORT, capture=True, check=False)
    except ComposeError:
        return LIST_SET_ASIDE
    if "Unknown command" in (result.stderr or ""):
        return (
            "this neops-core has no index_sync_status command: the rebuild's output above is the only "
            "record of which documents were set aside"
        )
    count = _rejected_count(result.stdout or "")
    if not count:
        return LIST_SET_ASIDE
    if count == 1:
        return f"1 row is now marked rejected; list it with: {LIST_REJECTED}"
    return f"{count} rows are now marked rejected; list them with: {LIST_REJECTED}"


def _rejected_count(stdout: str) -> int | None:
    """Only the number is at stake, so a report this release cannot read just leaves it out."""
    report = _last_json_line(stdout)
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
