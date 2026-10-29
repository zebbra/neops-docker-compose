"""Grafana gets an overlay of its own, the CMS's logging keys get the shared NEOPS_LOG_* names.

compose.metrics.yaml now requires compose.grafana.yaml, so an existing metrics install has it
added to COMPOSE_FILE. The CMS no longer reads NEOPS_LOGGER_CLASS or EXECUTION_LOG_RETENTION_*
and refuses to start while they are set, and its retention default drops from 90 to 30 days:
the horizon an install had is written out so the upgrade never shortens it.

Idempotent: every step checks what it would change. VictoriaLogs runs as root, so its data
directory needs no chown.
"""

DESCRIPTION = "add compose.grafana.yaml, rename the CMS logging keys, create data/logs/victoria"
IDEMPOTENT = True

GRAFANA = "compose.grafana.yaml"
METRICS = "compose.metrics.yaml"
OLD_CLASS = "NEOPS_LOGGER_CLASS"
LOG_CLASS = "NEOPS_LOG_CLASS"
LOG_BACKEND = "NEOPS_LOG_BACKEND"
OLD_ENABLED = "EXECUTION_LOG_RETENTION_ENABLED"
ENABLED = "NEOPS_LOG_RETENTION_ENABLED"
OLD_DAYS = "EXECUTION_LOG_RETENTION_DAYS"
RETENTION = "NEOPS_LOG_RETENTION"
PREVIOUS_DEFAULT_HORIZON = "90d"
CMS_TRUE_STRINGS = {"true", "on", "ok", "y", "yes", "1"}


def apply(ctx) -> None:
    from neops_compose.paths import Paths

    for d in Paths.for_repo(ctx.repo, ctx.env).data_dirs():
        ctx.mkdir(d)
    _add_grafana_overlay(ctx)
    _move_logger_class(ctx)
    _move(ctx.env, OLD_ENABLED, ENABLED)
    _carry_retention_over(ctx)


def _add_grafana_overlay(ctx) -> None:
    from neops_compose.scenario import Scenario

    files = list(Scenario.from_env(ctx.env).files)
    if METRICS not in files or GRAFANA in files:
        return
    files.insert(files.index(METRICS), GRAFANA)
    ctx.env.set("COMPOSE_FILE", ctx.env.get("COMPOSE_PATH_SEPARATOR", ":").join(files))
    ctx.log(f"COMPOSE_FILE: added {GRAFANA} before {METRICS}")


def _move_logger_class(ctx) -> None:
    """Compose never passed a remote store's URL to the CMS, so DbLogger, OptimizedDbLogger
    or a class that cannot have worked are the only states an install can be in."""
    old = ctx.env.get(OLD_CLASS)
    name = old.rsplit(".", 1)[-1]
    if name == "OptimizedDbLogger":
        _move(ctx.env, OLD_CLASS, LOG_CLASS)
    elif old and name != "DbLogger":
        ctx.log(f"{OLD_CLASS}={old} is not supported by compose; the CMS now uses {LOG_BACKEND}=db")
    ctx.env.unset(OLD_CLASS)
    if not ctx.env.is_set(LOG_BACKEND):
        ctx.env.set(LOG_BACKEND, "db")


def _carry_retention_over(ctx) -> None:
    horizon = _previous_horizon(ctx.env)
    if horizon and not ctx.env.is_set(RETENTION):
        ctx.env.set(RETENTION, horizon)
        ctx.log(f"{RETENTION}={horizon}, the retention this install had")
    ctx.env.unset(OLD_DAYS)


def _previous_horizon(env) -> str:
    if env.is_set(OLD_DAYS):
        return f"{env.get(OLD_DAYS).strip()}d"
    if _cms_reads_as_on(env.get(ENABLED)):
        return PREVIOUS_DEFAULT_HORIZON
    return ""


def _cms_reads_as_on(value: str) -> bool:
    """django-environ's env.bool, the CMS's reader for this toggle; wider than Env.flag."""
    try:
        return int(value) != 0
    except ValueError:
        return value.strip().lower() in CMS_TRUE_STRINGS


def _move(env, old: str, new: str) -> None:
    """Rename in place where possible, so the line keeps its position and its comment."""
    if env.is_set(old) and new not in env.values:
        env.rename(old, new)
        return
    if env.is_set(old) and not env.is_set(new):
        env.set(new, env.get(old))
    env.unset(old)
