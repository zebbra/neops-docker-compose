import pytest
import yaml

from neops_compose import databases, rotate, secrets
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


BASE_ENV = "COMPOSE_FILE=compose.yaml\nNEOPS_CMS_DB_PASSWORD=old-pw\n"
URLS = (
    "NEOPS_WEB_URL=https://neops.example.com\n"
    "NEOPS_CMS_URL=https://cms.neops.example.com\n"
    "NEOPS_ENGINE_URL=https://engine.neops.example.com\n"
    "NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com\n"
)


def make_ctx(tmp_path, services=NON_METRICS, env_text=BASE_ENV) -> Ctx:
    (tmp_path / ".env").write_text(env_text)
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


def test_public_hosts_covers_every_overlay_that_adds_a_hostname(tmp_path):
    """The self-signed certificate is minted for exactly this list, so a host missing here is
    a browser warning on that one service and nothing else to tell you why."""
    plain = make_ctx(tmp_path, env_text=BASE_ENV + URLS)
    assert rotate.public_hosts(plain.env, plain.scenario) == [
        "cms.neops.example.com",
        "engine.neops.example.com",
        "neops.example.com",
        "workflows.neops.example.com",
    ]

    everything = make_ctx(
        tmp_path,
        env_text="COMPOSE_FILE=compose.yaml:compose.oidc.yaml:compose.keycloak.yaml:compose.metrics.yaml\n"
        + URLS
        + "NEOPS_KEYCLOAK_URL=https://auth.neops.example.com\n"
        "NEOPS_GRAFANA_URL=https://grafana.neops.example.com\n",
    )
    hosts = rotate.public_hosts(everything.env, everything.scenario)
    assert "auth.neops.example.com" in hosts and "grafana.neops.example.com" in hosts


def test_public_hosts_skips_grafana_while_it_stays_on_loopback(tmp_path):
    """The metrics overlay routes Grafana publicly only when NEOPS_GRAFANA_URL is set."""
    ctx = make_ctx(tmp_path, env_text="COMPOSE_FILE=compose.yaml:compose.metrics.yaml\n" + URLS)
    assert not any("grafana" in host for host in rotate.public_hosts(ctx.env, ctx.scenario))


def test_admin_password_is_set_through_the_environment_and_recorded(tmp_path):
    """The new password must not appear in argv: `docker compose exec`'s arguments are visible
    in the host's process table to every local user."""
    ctx = make_ctx(tmp_path)
    rotate.admin_password(ctx, "s3cret-value")
    assert ctx.env.values["NEOPS_ADMIN_PASSWORD"] == "s3cret-value"
    service, cmd, env = ctx.compose.execs[0]
    assert service == "cms" and cmd[:4] == ["python", "manage.py", "shell", "-c"]
    assert "set_password(os.environ['P'])" in cmd[-1]
    assert env == {"U": "neops", "P": "s3cret-value"}
    assert not any("s3cret-value" in part for part in cmd)
    assert not any("s3cret-value" in message for message in ctx.log)


def test_admin_password_addresses_the_configured_admin_user(tmp_path):
    ctx = make_ctx(tmp_path, env_text=BASE_ENV + "NEOPS_ADMIN_USER=operator\n")
    rotate.admin_password(ctx, "pw")
    assert ctx.compose.execs[0][2]["U"] == "operator"


def test_secret_key_rotates_the_engine_token_too(tmp_path, monkeypatch):
    """Static API keys are signed with DJANGO_SECRET_KEY, the engine's own among them: leaving
    it behind takes the engine down until someone works out why its token stopped working."""
    ctx = make_ctx(tmp_path)
    rotated = []
    monkeypatch.setattr(rotate.token, "rotate_engine_token", rotated.append)
    rotate.secret_key(ctx)
    new = ctx.env.values["DJANGO_SECRET_KEY"]
    assert len(new) == 64
    assert ctx.compose.ups == [list(rotate.CORE_SERVICES)]
    assert rotated == [ctx]


def test_jwt_regenerates_the_keypair_and_restarts_both_sides_of_it(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    secrets.ensure_jwt(ctx.paths.jwt_dir)
    before = (ctx.paths.jwt_dir / "private.pem").read_bytes()
    rotate.jwt(ctx)
    after = (ctx.paths.jwt_dir / "private.pem").read_bytes()
    assert after != before and (ctx.paths.jwt_dir / "public.pem").exists()
    assert ctx.compose.ups == [["cms", "cms-worker", "cms-beat", "engine"]]


def test_tls_reissues_the_certificate_for_the_hostnames_in_env_now(tmp_path):
    """Adding a hostname to .env is exactly when this is run, so it has to re-read the list
    rather than reuse whatever SANs the old certificate carried."""
    ctx = make_ctx(
        tmp_path,
        services=(*NON_METRICS, "traefik"),
        env_text="COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml\n"
        "NEOPS_TLS_SELF_SIGNED=true\n" + URLS,
    )
    secrets.ensure_selfsigned(ctx.paths.tls_dir, ["neops.example.com"])
    rotate.tls(ctx)
    assert secrets.selfsigned_sans(ctx.paths.tls_dir / "cert.pem") == set(
        rotate.public_hosts(ctx.env, ctx.scenario)
    )
    assert ctx.compose.ups == [["traefik"]]
