import pytest
import yaml

from neops_compose import databases, rotate
from neops_compose.compose import ComposeError
from neops_compose.context import Ctx
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.state import State

NON_METRICS = (
    "postgres-cms",
    "postgres-engine",
    "redis",
    "elasticsearch",
    "cms-init",
    "cms",
    "cms-worker",
    "cms-beat",
    "engine",
    "monitor",
    "worker",
    "web",
)


class Recorder(list):
    def __call__(self, message: str) -> None:
        self.append(message)


class RecordingCompose:
    def __init__(self, services):
        self.services = list(services)
        self.execs = []
        self.ups = []

    def service_names(self):
        return list(self.services)

    def exec(self, service, *cmd, env=None):
        self.execs.append((service, list(cmd), dict(env or {})))
        return ""

    def up(self, *services, wait=True, force_recreate=False):
        self.ups.append(list(services))


def make_ctx(tmp_path, services=NON_METRICS) -> Ctx:
    (tmp_path / ".env").write_text("COMPOSE_FILE=compose.yaml\nNEOPS_CMS_DB_PASSWORD=old-pw\n")
    env = Env(tmp_path / ".env")
    return Ctx(
        repo=tmp_path,
        env=env,
        paths=Paths.for_repo(tmp_path, env),
        scenario=Scenario.from_env(env),
        compose=RecordingCompose(services),
        state=State(),
        log=Recorder(),
    )


def test_every_service_rotate_recreates_exists_in_some_compose_file(repo):
    declared = set()
    for path in sorted(repo.glob("compose*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        declared.update((doc.get("services") or {}).keys())
    wanted = set(rotate.CORE_SERVICES)
    wanted.update(database.service for database in databases.DATABASES)
    for dependants in rotate.DEPENDANTS.values():
        wanted.update(dependants)
    assert wanted <= declared, sorted(wanted - declared)


def test_every_database_the_cli_knows_can_be_rotated():
    """One table, three consumers: a database preflight probes and backup dumps but rotate
    cannot reach would desync silently."""
    assert set(rotate.DEPENDANTS) == set(databases.BY_KEY)


def test_db_password_keeps_the_new_password_out_of_argv(tmp_path):
    ctx = make_ctx(tmp_path)
    rotate.db_password(ctx, "cms")
    new = ctx.env.values["NEOPS_CMS_DB_PASSWORD"]
    assert new != "old-pw" and len(new) == 64
    assert len(ctx.compose.execs) == 1
    service, cmd, env = ctx.compose.execs[0]
    assert service == "postgres-cms"
    assert not any(new in part for part in cmd), cmd
    assert env["NEOPS_NEW_PASSWORD"] == new and env["PGPASSWORD"] == "old-pw"
    assert "$NEOPS_NEW_PASSWORD" in cmd[-1]


def test_db_password_recreates_exactly_the_dependants_present_in_the_scenario(tmp_path):
    """Spelled out rather than re-derived from DEPENDANTS: this is the list an operator's
    deployment restarts, and a table edit that changes it should fail here."""
    ctx = make_ctx(tmp_path)
    rotate.db_password(ctx, "cms")
    assert ctx.compose.ups == [["cms-init", "cms", "cms-worker", "cms-beat"]]


def test_db_password_reports_a_failure_without_echoing_psql(tmp_path):
    ctx = make_ctx(tmp_path)

    def boom(service, *cmd, env=None):
        raise ComposeError("psql: ALTER ROLE neops WITH PASSWORD 'leaked-secret' failed")

    ctx.compose.exec = boom
    with pytest.raises(rotate.RotateError) as exc:
        rotate.db_password(ctx, "cms")
    message = str(exc.value)
    assert "leaked-secret" not in message
    assert "logs" not in message, "the postgres log echoes the failed statement and its password"
    assert "postgres-cms" in message and "retry with ./neops rotate db-password" in message
    assert ctx.env.values["NEOPS_CMS_DB_PASSWORD"] == "old-pw"


def test_recreate_warns_about_a_service_this_scenario_does_not_have(tmp_path):
    ctx = make_ctx(tmp_path)
    rotate._recreate(ctx, ("cms", "postgres-exporter-cms"))
    assert any("postgres-exporter-cms" in m for m in ctx.log)
    assert ctx.compose.ups == [["cms"]]


def test_tls_refuses_to_touch_an_operator_supplied_certificate(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(rotate.RotateError, match="self-signed"):
        rotate.tls(ctx)
