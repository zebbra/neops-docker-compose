"""Give Grafana's bind mount to the uid the Grafana image runs as.

grafana/grafana runs as uid 472 and refuses to start when GF_PATHS_DATA is not
writable, so the directory 0001 created as the operator's uid has to change hands.
Harmless on a deployment without the metrics overlay: the directory exists either way.
"""

from pathlib import Path

DESCRIPTION = "chown data/metrics/grafana to the Grafana uid"
IDEMPOTENT = True
GRAFANA_UID = 472


def apply(ctx) -> None:
    from neops_compose.paths import Paths

    paths = Paths.for_repo(ctx.repo, ctx.env)
    grafana: Path = paths.data / "metrics" / "grafana"
    ctx.mkdir(grafana)
    ctx.ensure_owner(grafana, GRAFANA_UID, 0)
