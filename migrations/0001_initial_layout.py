"""Create the data tree every bind mount points at.

Idempotent: directories that exist are left alone. Elasticsearch runs as uid 1000
and cannot create its data directory itself, so it is chowned through a throwaway
postgres:16-alpine container: no sudo needed, and the stack pulls that image anyway.
"""

from pathlib import Path

DESCRIPTION = "create the data/ tree and the state directory"
IDEMPOTENT = True


def apply(ctx) -> None:
    from neops_compose.paths import Paths

    paths = Paths.for_repo(ctx.repo, ctx.env)
    for d in paths.data_dirs():
        ctx.mkdir(d)
    for d in paths.private_dirs():
        ctx.mkdir(d, mode=0o700)
    es: Path = paths.data / "elasticsearch"
    stat = es.stat()
    if (stat.st_uid, stat.st_gid) != (1000, 0):
        ctx.chown_via_container(es, 1000, 0)
