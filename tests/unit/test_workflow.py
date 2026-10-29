import json
import os
import subprocess

import pytest

from neops_compose import preflight, search_index, secrets, workflow
from neops_compose.compose import DOCKER_MISSING, ComposeError
from neops_compose.context import Ctx
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.state import State


def test_tag_of():
    assert workflow.tag_of("quay.io/zebbra/neops-core:2.1.0-beta.5") == "2.1.0-beta.5"
    assert workflow.tag_of("neops-core") == ""


def test_downgrade_detection():
    assert workflow.is_downgrade("2.1.0-beta.5", "2.1.0-beta.4") is True
    assert workflow.is_downgrade("2.1.0-beta.5", "2.1.0") is False
    assert workflow.is_downgrade("2.1.0", "2.0.9") is True
    assert workflow.is_downgrade("2.1.0", "cutting-edge") is False  # unparsable: never block
    assert workflow.is_downgrade("cutting-edge", "2.1.0") is False
    assert workflow.is_downgrade("2.1.0-beta.2", "2.1.0-rc.1") is False
    assert workflow.is_downgrade("2.1.0-rc.1", "2.1.0-beta.2") is True
    assert workflow.is_downgrade("2.1.0", "2.1.0") is False


def test_guard_downgrade_raises_only_for_core(tmp_path):
    last = {
        "images": {
            "cms": "quay.io/zebbra/neops-core:2.1.0",
            "engine": "quay.io/zebbra/neops-workflow-engine:0.43.0",
        }
    }
    now = {
        "cms": "quay.io/zebbra/neops-core:2.0.9",
        "engine": "quay.io/zebbra/neops-workflow-engine:0.42.0",
    }
    with pytest.raises(workflow.Downgrade, match="neops-core"):
        workflow.guard_downgrade(last, now, allow=False)
    workflow.guard_downgrade(last, now, allow=True)
    workflow.guard_downgrade(None, now, allow=False)


class FakeCompose:
    def __init__(self, image="quay.io/zebbra/neops-core:2.1.0"):
        self.image = image
        self.calls: list[str] = []

    def ps(self):
        return [{"Service": "cms", "Image": self.image, "State": "running"}]

    def images(self):
        return [self.image]

    def pull(self):
        self.calls.append("pull")

    def up(self, *services, **kwargs):
        self.calls.append("up" if not services else "up:" + ",".join(services))

    def exec(self, service, *cmd, env=None):
        """`elastic_index --help` of a core older than --rebuild-if-stale and --sync-lookups."""
        return "usage: manage.py elastic_index [--create] [--populate] [--delete] [--rebuild]"

    def run(self, *args, capture=False, check=True, env=None):
        return subprocess.CompletedProcess(args, 0)


def make_ctx(tmp_path, env_text="COMPOSE_FILE=compose.yaml\n", compose=None) -> Ctx:
    (tmp_path / ".env").write_text(env_text)
    env = Env(tmp_path / ".env")
    return Ctx(
        repo=tmp_path,
        env=env,
        paths=Paths.for_repo(tmp_path, env),
        scenario=Scenario.from_env(env),
        compose=compose or FakeCompose(),
        state=State(),
        log=lambda m: None,
    )


def test_finish_records_the_running_images_before_doctor_runs(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)

    def exploding_doctor(*args, **kwargs):
        raise RuntimeError("doctor could not reach the deployment")

    monkeypatch.setattr(workflow, "doctor", exploding_doctor)
    with pytest.raises(RuntimeError, match="could not reach"):
        workflow._finish(ctx, None, False)
    assert ctx.state.last_up["images"] == {"cms": "quay.io/zebbra/neops-core:2.1.0"}
    assert ctx.state.last_up["doctor_ok"] is False
    assert State.load(ctx.paths.state_file).last_up["images"]["cms"].endswith("2.1.0")


def test_finish_records_a_failed_doctor_and_still_blocks(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr(workflow, "doctor", lambda *a, **k: False)
    with pytest.raises(workflow.Blocked):
        workflow._finish(ctx, None, False)
    assert State.load(ctx.paths.state_file).last_up["doctor_ok"] is False


def test_finish_marks_a_passing_doctor(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    monkeypatch.setattr(workflow, "doctor", lambda *a, **k: True)
    workflow._finish(ctx, None, False)
    assert State.load(ctx.paths.state_file).last_up["doctor_ok"] is True


def test_status_separates_a_missing_verdict_from_a_failed_one():
    assert workflow._verdict({"at": "x", "images": {}}) == "doctor: not recorded"
    assert workflow._verdict({"at": "x", "doctor_ok": False}) == "doctor FAILED"
    assert workflow._verdict({"at": "x", "doctor_ok": True}) == "doctor ok"


def check_with(tmp_path, monkeypatch, env_text: str) -> list[str]:
    """`check` is the one path `check`, `install` and `up` all take, so its log is where a
    deployment-wide warning has to appear to be seen by any of them."""
    logged: list[str] = []
    ctx = make_ctx(tmp_path, env_text)
    ctx.log = logged.append
    monkeypatch.setattr(preflight, "run_checks", lambda *a, **k: [preflight.Check("stub", True)])
    workflow.check(ctx, check_images=False)
    return logged


def test_check_warns_when_the_security_checks_are_disabled(tmp_path, monkeypatch):
    logged = check_with(
        tmp_path, monkeypatch, "COMPOSE_FILE=compose.yaml\nNEOPS_DISABLE_SECURITY_CHECKS=true\n"
    )
    assert any("NEOPS_DISABLE_SECURITY_CHECKS" in line for line in logged)


def test_check_says_nothing_about_the_flag_when_it_is_off(tmp_path, monkeypatch):
    logged = check_with(tmp_path, monkeypatch, "COMPOSE_FILE=compose.yaml\n")
    assert logged and not any("NEOPS_DISABLE_SECURITY_CHECKS" in line for line in logged)


def test_install_checks_images_only_after_render(tmp_path, monkeypatch):
    """docker compose cannot resolve the image list until render has written generated/,
    so a first install that checked images up front could never pass its own preflight."""
    ctx = make_ctx(tmp_path)
    order: list[str] = []
    monkeypatch.setattr(
        workflow, "check", lambda c, check_images=True: order.append(f"check(images={check_images})")
    )
    monkeypatch.setattr(workflow, "migrate_all", lambda c: order.append("migrate"))
    monkeypatch.setattr(workflow, "keys", lambda c: order.append("keys"))
    monkeypatch.setattr(workflow, "render", lambda *a: order.append("render"))
    monkeypatch.setattr(workflow, "check_images", lambda c: order.append("check_images"))
    monkeypatch.setattr(workflow.token, "ensure_engine_token", lambda *a: order.append("token"))
    monkeypatch.setattr(workflow, "_finish", lambda *a: order.append("finish"))

    workflow.install(ctx)

    assert order[0] == "check(images=False)"
    assert order.index("render") < order.index("check_images")


CMS_FIRST = "up:" + ",".join(workflow.CMS_FIRST)


def _with_engine_token(ctx: Ctx) -> Ctx:
    ctx.paths.engine_env.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.engine_env.write_text("NEOPS_CMS_TOKEN=already-minted\n")
    return ctx


def _stub_up(monkeypatch, order: list[str], minted: bool = False) -> None:
    monkeypatch.setattr(
        workflow, "check", lambda c, check_images=True: order.append(f"check(images={check_images})")
    )
    monkeypatch.setattr(workflow, "migrate_all", lambda c: order.append("migrate"))
    monkeypatch.setattr(workflow, "keys", lambda c: order.append("keys"))
    monkeypatch.setattr(workflow, "render", lambda *a: order.append("render"))
    monkeypatch.setattr(workflow, "_finish", lambda *a: order.append("finish"))
    monkeypatch.setattr(workflow.token, "ensure_engine_token", lambda ctx: minted)


def test_up_checks_without_images_and_guards_before_it_changes_anything(tmp_path, monkeypatch):
    """The downgrade guard has to run before migrate and render, because both write to the
    deployment: refusing afterwards leaves the operator half-moved to a release we refused."""
    order: list[str] = []
    _stub_up(monkeypatch, order)
    ctx = make_ctx(tmp_path, compose=FakeCompose("quay.io/zebbra/neops-core:2.0.9"))
    ctx.state.record_up({"cms": "quay.io/zebbra/neops-core:2.1.0"}, doctor_ok=True)

    with pytest.raises(workflow.Downgrade, match="neops-core"):
        workflow.up(ctx)

    assert order == ["check(images=False)"]
    assert ctx.compose.calls == []


def test_up_pulls_and_starts_once_when_the_engine_token_is_already_valid(tmp_path, monkeypatch):
    order: list[str] = []
    _stub_up(monkeypatch, order, minted=False)
    ctx = _with_engine_token(make_ctx(tmp_path))
    workflow.up(ctx)
    assert order == ["check(images=False)", "migrate", "keys", "render", "finish"]
    assert ctx.compose.calls == ["pull", "up"]


def test_up_starts_a_second_time_when_a_token_was_minted(tmp_path, monkeypatch):
    """The engine reads its CMS token from an env file at container start, so a token minted
    after the first `up` only reaches it through a second one."""
    ctx = _with_engine_token(make_ctx(tmp_path))
    _stub_up(monkeypatch, [], minted=True)
    workflow.up(ctx)
    assert ctx.compose.calls == ["pull", "up", "up"]


def test_a_first_up_never_asks_compose_for_images(tmp_path, monkeypatch):
    """`docker compose config` cannot load a deployment whose generated/ does not exist yet,
    and there is no previous start to compare against anyway."""

    class NoConfigYet(FakeCompose):
        def images(self):
            raise AssertionError("compose config was asked before render")

    _stub_up(monkeypatch, [], minted=True)
    workflow.up(make_ctx(tmp_path, compose=NoConfigYet()))


def test_a_first_up_brings_the_cms_up_alone_before_the_engine(tmp_path, monkeypatch):
    """`up` as the very first command, without `install`: the engine refuses its placeholder
    token, and `up --wait` on the whole stack would wait on that restart loop for the whole
    timeout. Without a token the start is the two-phase one install does."""
    ctx = make_ctx(tmp_path)
    _stub_up(monkeypatch, [], minted=True)
    workflow.up(ctx)
    assert ctx.compose.calls == ["pull", CMS_FIRST, "up"]


def test_install_starts_the_cms_alone_first_and_everything_once_the_token_exists(tmp_path, monkeypatch):
    ctx = make_ctx(tmp_path)
    _stub_up(monkeypatch, [], minted=True)
    monkeypatch.setattr(workflow, "check_images", lambda c: None)
    workflow.install(ctx)
    assert ctx.compose.calls == ["pull", CMS_FIRST, "up"]
    _stub_up(monkeypatch, [], minted=False)
    ctx.compose.calls.clear()
    workflow.install(_with_engine_token(ctx))
    assert ctx.compose.calls == ["pull", "up"]


def test_up_never_reaches_the_images_check_that_needs_a_rendered_generated_tree(tmp_path, monkeypatch):
    """`up` leaves image resolution to `check`: repeating it here would make every start
    depend on the registry."""
    calls: list[str] = []
    monkeypatch.setattr(workflow, "check_images", lambda c: calls.append("check_images"))
    _stub_up(monkeypatch, [])
    workflow.up(make_ctx(tmp_path))
    assert calls == []


ES_OLD = "docker.elastic.co/elasticsearch/elasticsearch:8.9.2"
ES_NEW = "docker.elastic.co/elasticsearch/elasticsearch:8.19.23"
CORE = "quay.io/zebbra/neops-core:2.1.0"
CORE_PREV = "quay.io/zebbra/neops-core:2.0.0"
DECIDING_CORE = "usage: manage.py elastic_index [--rebuild] [--rebuild-if-stale] [--sync-lookups]"
OLD_CORE_WITH_LOOKUPS = "usage: manage.py elastic_index [--rebuild] [--sync-lookups]"
ELASTIC_INDEX = ("python", "-u", "manage.py", "elastic_index")
EXEC = ("exec", "-T", "cms", *ELASTIC_INDEX)
REBUILD_IF_STALE = (*EXEC, "--rebuild-if-stale")
REBUILD = (*EXEC, "--rebuild", "--force")


def _last_up(cms=CORE, es=ES_NEW) -> dict:
    state = State()
    state.record_up({"cms": cms, "elasticsearch": es}, doctor_ok=True)
    return state.last_up


def test_images_moved_when_core_or_elasticsearch_changed():
    assert search_index.images_moved(_last_up(es=ES_OLD), {"cms": CORE, "elasticsearch": ES_NEW})
    assert search_index.images_moved(_last_up(cms=CORE_PREV), {"cms": CORE, "elasticsearch": ES_NEW})
    assert not search_index.images_moved(_last_up(), {"cms": CORE, "elasticsearch": ES_NEW, "engine": "x"})


def test_a_fresh_install_has_nothing_to_rebuild():
    assert not search_index.images_moved(None, {"cms": CORE, "elasticsearch": ES_NEW})


INDEX_SYNC_REPORT = ("exec", "-T", "cms", "python", "manage.py", "index_sync_status", "--json")
REJECTED_REPORT = "neops.enterprise.celery.cron loaded\n" + json.dumps(
    {"version": 1, "models": {"core.Device": {"rejected": 2}, "core.Interface": {"rejected": 3}}}
)


class IndexedCompose(FakeCompose):
    def __init__(
        self,
        help_text: str,
        exits: dict[tuple[str, ...], int] | None = None,
        help_fails: bool = False,
        sync_report: str = REJECTED_REPORT,
        sync_stderr: str = "rows are rejected",
        sync_raises: BaseException | None = None,
    ):
        super().__init__(CORE)
        self.help_text = help_text
        self.exits = exits or {}
        self.help_fails = help_fails
        self.sync_report = sync_report
        self.sync_stderr = sync_stderr
        self.sync_raises = sync_raises
        self.execs: list[tuple[str, ...]] = []
        self.runs: list[tuple[tuple[str, ...], bool]] = []

    def ps(self):
        return super().ps() + [{"Service": "elasticsearch", "Image": ES_NEW, "State": "running"}]

    def exec(self, service, *cmd, env=None):
        self.execs.append((service, *cmd))
        if self.help_fails:
            raise ComposeError("docker compose exec -T cms ... failed (1) service cms is not running")
        return self.help_text

    def run(self, *args, capture=False, check=True, env=None):
        self.runs.append((args, capture))
        if args == INDEX_SYNC_REPORT:
            if self.sync_raises:
                raise self.sync_raises
            return subprocess.CompletedProcess(args, 1, stdout=self.sync_report, stderr=self.sync_stderr)
        return subprocess.CompletedProcess(args, self.exits.get(args, 0))

    @property
    def streamed(self) -> list[tuple[str, ...]]:
        """Every elastic_index call must stream: a rebuild runs for minutes."""
        rebuilds = [(args, capture) for args, capture in self.runs if args[: len(EXEC)] == EXEC]
        assert all(capture is False for _, capture in rebuilds)
        return [args for args, _ in rebuilds]


def _up_after(
    tmp_path,
    monkeypatch,
    compose: IndexedCompose,
    last_up: dict | None,
    order: list[str] | None = None,
    logged: list[str] | None = None,
) -> list[str]:
    order = [] if order is None else order
    _stub_up(monkeypatch, order)
    run = compose.run

    def recorded_run(*args, **kwargs):
        order.append("run:" + " ".join(args[len(EXEC) :]))
        return run(*args, **kwargs)

    monkeypatch.setattr(compose, "run", recorded_run)
    ctx = _with_engine_token(make_ctx(tmp_path, compose=compose))
    ctx.state.last_up = last_up
    if logged is not None:
        ctx.log = logged.append
    workflow.up(ctx)
    return order


def test_a_deciding_core_is_asked_on_every_up_and_before_the_start_is_recorded(tmp_path, monkeypatch):
    """Core compares each index's stamp itself, so compose asks even when no image moved."""
    compose = IndexedCompose(DECIDING_CORE)
    logged: list[str] = []
    order = _up_after(tmp_path, monkeypatch, compose, _last_up(), logged=logged)

    assert compose.execs == [("cms", *ELASTIC_INDEX, "--help")]
    assert compose.streamed == [REBUILD_IF_STALE]
    assert order[-2:] == ["run:--rebuild-if-stale", "finish"]
    assert not any("set aside" in line for line in logged)
    assert all(args != INDEX_SYNC_REPORT for args, _ in compose.runs)


def _set_aside(tmp_path, monkeypatch, **compose_options) -> tuple[IndexedCompose, list[str], list[str]]:
    """Exit 3: core rebuilt everything and set aside what Elasticsearch refused."""
    compose = IndexedCompose(DECIDING_CORE, exits={REBUILD_IF_STALE: 3}, **compose_options)
    logged: list[str] = []
    order = _up_after(tmp_path, monkeypatch, compose, _last_up(), logged=logged)
    return compose, order, logged


def test_documents_set_aside_by_the_rebuild_warn_and_the_start_is_still_recorded(tmp_path, monkeypatch):
    compose, order, logged = _set_aside(tmp_path, monkeypatch)

    assert order[-1] == "finish"
    assert compose.runs[-1] == (INDEX_SYNC_REPORT, True)
    warning, rows = logged[-2:]
    assert warning.startswith("WARNING:") and "set them aside" in warning and "output above" in warning
    assert rows == f"5 rows are now marked rejected; list them with: {search_index.LIST_REJECTED}"


def test_the_warning_is_out_before_the_count_is_asked_for(tmp_path, monkeypatch):
    """Nothing bounds the count call in time; an operator who gives up on it has the warning."""
    compose = IndexedCompose(DECIDING_CORE, exits={REBUILD_IF_STALE: 3}, sync_raises=KeyboardInterrupt())
    logged: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        _up_after(tmp_path, monkeypatch, compose, _last_up(), logged=logged)
    assert logged[-1].startswith("WARNING:")


def test_one_row_set_aside_is_counted_in_the_singular(tmp_path, monkeypatch):
    one = json.dumps({"version": 1, "models": {"core.Device": {"rejected": 1}}})
    _, _, logged = _set_aside(tmp_path, monkeypatch, sync_report=one)

    assert logged[-1] == f"1 row is now marked rejected; list it with: {search_index.LIST_REJECTED}"


@pytest.mark.parametrize(
    "sync_report",
    [
        json.dumps({"version": 2, "models": {"core.Device": {"rejected": 2}}}),
        json.dumps({"version": 1, "models": {"core.Device": {"rejected": 0}}}),
        "no report at all",
    ],
)
def test_without_a_count_to_give_the_warning_still_points_at_the_listing(tmp_path, monkeypatch, sync_report):
    """An unreadable report, or a row already re-indexed since, only costs the number."""
    _, order, logged = _set_aside(tmp_path, monkeypatch, sync_report=sync_report)

    assert order[-1] == "finish"
    assert logged[-1] == search_index.LIST_SET_ASIDE


def test_a_count_that_cannot_be_asked_for_still_lets_the_start_be_recorded(tmp_path, monkeypatch):
    _, order, logged = _set_aside(tmp_path, monkeypatch, sync_raises=ComposeError(DOCKER_MISSING))

    assert order[-1] == "finish"
    assert logged[-2].startswith("WARNING:")
    assert logged[-1] == search_index.LIST_SET_ASIDE


def test_a_core_without_index_sync_status_is_sent_to_the_rebuild_output(tmp_path, monkeypatch):
    _, order, logged = _set_aside(tmp_path, monkeypatch, sync_stderr="Unknown command: 'index_sync_status'")

    assert order[-1] == "finish"
    assert "only record" in logged[-1] and "index_sync_status --list" not in logged[-1]


def test_a_deciding_core_is_asked_on_a_fresh_install_too(tmp_path, monkeypatch):
    compose = IndexedCompose(DECIDING_CORE)
    _up_after(tmp_path, monkeypatch, compose, None)

    assert compose.streamed == [REBUILD_IF_STALE]


def test_install_records_the_start_after_documents_were_set_aside(tmp_path, monkeypatch):
    order: list[str] = []
    _stub_up(monkeypatch, order)
    monkeypatch.setattr(workflow, "check_images", lambda c: None)
    compose = IndexedCompose(DECIDING_CORE, exits={REBUILD_IF_STALE: 3})
    ctx = _with_engine_token(make_ctx(tmp_path, compose=compose))
    ctx.state.last_up = _last_up()

    workflow.install(ctx)

    assert order[-1] == "finish"
    assert compose.runs[-1] == (INDEX_SYNC_REPORT, True)


def test_a_failed_rebuild_stops_up_before_it_records_the_start(tmp_path, monkeypatch):
    compose = IndexedCompose(DECIDING_CORE, exits={REBUILD_IF_STALE: 1})
    order: list[str] = []
    with pytest.raises(ComposeError, match=r"--rebuild-if-stale exited 1.*not recorded"):
        _up_after(tmp_path, monkeypatch, compose, _last_up(), order)
    assert order[-1] == "run:--rebuild-if-stale"


def test_a_failed_help_stops_up_before_it_records_the_start(tmp_path, monkeypatch):
    compose = IndexedCompose(DECIDING_CORE, help_fails=True)
    order: list[str] = []
    with pytest.raises(ComposeError):
        _up_after(tmp_path, monkeypatch, compose, _last_up(), order)
    assert "finish" not in order
    assert compose.runs == []


def test_an_older_core_is_rebuilt_whole_after_elasticsearch_moved(tmp_path, monkeypatch):
    compose = IndexedCompose(OLD_CORE_WITH_LOOKUPS)
    order = _up_after(tmp_path, monkeypatch, compose, _last_up(es=ES_OLD))

    assert compose.streamed == [(*REBUILD, "--sync-lookups")]
    assert order[-2:] == ["run:--rebuild --force --sync-lookups", "finish"]


def test_an_older_core_is_rebuilt_whole_after_a_core_upgrade(tmp_path, monkeypatch):
    """An older core cannot say whether its upgrade changed the indices, so it is rebuilt."""
    compose = IndexedCompose("usage: manage.py elastic_index [--rebuild]")
    _up_after(tmp_path, monkeypatch, compose, _last_up(cms=CORE_PREV))

    assert compose.streamed == [REBUILD]


def test_an_older_cores_full_rebuild_tolerates_no_exit_but_0(tmp_path, monkeypatch):
    """Exit 3 means documents set aside only for --rebuild-if-stale; on --rebuild it is a failure."""
    compose = IndexedCompose(OLD_CORE_WITH_LOOKUPS, exits={(*REBUILD, "--sync-lookups"): 3})
    order: list[str] = []
    with pytest.raises(ComposeError, match=r"--rebuild --force --sync-lookups exited 3.*not recorded"):
        _up_after(tmp_path, monkeypatch, compose, _last_up(es=ES_OLD), order)
    assert "finish" not in order


def test_an_older_core_is_left_alone_while_neither_image_moved(tmp_path, monkeypatch):
    compose = IndexedCompose(OLD_CORE_WITH_LOOKUPS)
    _up_after(tmp_path, monkeypatch, compose, _last_up())

    assert compose.runs == []


def test_install_on_a_live_deployment_asks_core_as_up_does(tmp_path, monkeypatch):
    order: list[str] = []
    _stub_up(monkeypatch, order)
    monkeypatch.setattr(workflow, "check_images", lambda c: None)
    compose = IndexedCompose(DECIDING_CORE)
    ctx = _with_engine_token(make_ctx(tmp_path, compose=compose))
    ctx.state.last_up = _last_up()

    workflow.install(ctx)

    assert compose.streamed == [REBUILD_IF_STALE]
    assert order[-1] == "finish"


def test_a_state_file_with_the_retired_search_schema_upgrades_and_loses_it(tmp_path, monkeypatch):
    """state.json written by an earlier release recorded search_schema; it is ignored, then dropped."""
    compose = IndexedCompose(DECIDING_CORE)
    ctx = _with_engine_token(make_ctx(tmp_path, compose=compose))
    old = {"at": "2026-10-27T00:00:00Z", "images": {"cms": CORE, "elasticsearch": ES_NEW}, "doctor_ok": True}
    ctx.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.paths.state_file.write_text(json.dumps({"schema": 1, "last_up": {**old, "search_schema": "a" * 64}}))
    ctx.state = State.load(ctx.paths.state_file)
    finish = workflow._finish
    _stub_up(monkeypatch, [])
    monkeypatch.setattr(workflow, "_finish", finish)
    monkeypatch.setattr(workflow, "doctor", lambda *a, **k: True)

    workflow.up(ctx)

    assert "search_schema" not in State.load(ctx.paths.state_file).last_up
    assert compose.streamed == [REBUILD_IF_STALE]


def _selfsigned_env(host: str) -> str:
    return (
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml\n"
        "NEOPS_TLS_SELF_SIGNED=true\n"
        f"NEOPS_WEB_URL=https://{host}\n"
        f"NEOPS_CMS_URL=https://cms.{host}\n"
        f"NEOPS_ENGINE_URL=https://engine.{host}\n"
        f"NEOPS_WORKFLOWS_URL=https://workflows.{host}\n"
    )


def test_keys_mints_a_self_signed_certificate_for_every_public_host(tmp_path):
    ctx = make_ctx(tmp_path, _selfsigned_env("neops.example.com"))
    workflow.keys(ctx)
    assert secrets.selfsigned_sans(ctx.paths.tls_dir / "cert.pem") == {
        "neops.example.com",
        "cms.neops.example.com",
        "engine.neops.example.com",
        "workflows.neops.example.com",
    }
    assert (ctx.paths.jwt_dir / "private.pem").exists()


def test_keys_blocks_on_a_certificate_that_no_longer_covers_the_hostnames(tmp_path):
    """Renaming a host in .env makes the existing certificate wrong for it. Silently reusing
    it means Traefik serves a name the certificate does not carry, which only the browser
    tells you about; rotating it behind the operator's back throws away a CA they may have
    already distributed."""
    ctx = make_ctx(tmp_path, _selfsigned_env("neops.example.com"))
    workflow.keys(ctx)
    moved = make_ctx(tmp_path, _selfsigned_env("neops.example.org"))
    with pytest.raises(workflow.Blocked, match="rotate tls"):
        workflow.keys(moved)


def test_status_reports_the_scenario_images_and_migration_counts(tmp_path):
    ctx = make_ctx(tmp_path, "COMPOSE_FILE=compose.yaml:compose.expose.yaml\n")
    ctx.state.record_applied("0001_initial_layout")
    ctx.state.record_up({"cms": "quay.io/zebbra/neops-core:2.1.0"}, doctor_ok=True)
    text = workflow.status(ctx)
    assert "scenario: compose.yaml : compose.expose.yaml" in text
    assert f"data: {ctx.paths.data}" in text
    assert "  cms: quay.io/zebbra/neops-core:2.1.0" in text
    assert "migrations: 1 applied, 0 pending" in text
    assert "doctor ok" in text
    assert "FAKED" not in text


def test_status_names_a_faked_migration(tmp_path):
    """A faked migration means the deployment was hand-patched; status has to keep saying so."""
    ctx = make_ctx(tmp_path)
    ctx.state.record_faked("0002_grafana_data_owner")
    assert "1 FAKED (0002_grafana_data_owner)" in workflow.status(ctx)


def _purge_ctx(tmp_path, log):
    class FakeCompose:
        def __init__(self):
            self.downed = False

        def down(self):
            self.downed = True

    (tmp_path / ".env").write_text("COMPOSE_FILE=compose.yaml\n")
    env = Env(tmp_path / ".env")
    paths = Paths.for_repo(tmp_path, env)
    paths.data.mkdir(parents=True, exist_ok=True)
    paths.generated.mkdir(parents=True, exist_ok=True)
    return Ctx(tmp_path, env, paths, Scenario.from_env(env), FakeCompose(), State(), log)


def test_purge_claims_the_data_tree_back_before_deleting_it(tmp_path, monkeypatch):
    """Postgres writes data/ as uid 70 mode 0700, so rmtree as the operator cannot remove it."""
    claimed = []
    monkeypatch.setattr(workflow, "chown_via_container", lambda p, uid, gid: claimed.append((p, uid, gid)))
    ctx = _purge_ctx(tmp_path, lambda m: None)
    workflow.purge(ctx, str(ctx.paths.data))
    assert claimed == [(ctx.paths.data, os.getuid(), os.getgid())]
    assert ctx.compose.downed
    assert not ctx.paths.data.exists() and not ctx.paths.generated.exists()


def test_purge_without_the_exact_data_dir_removes_nothing(tmp_path, monkeypatch):
    claimed = []
    monkeypatch.setattr(workflow, "chown_via_container", lambda p, uid, gid: claimed.append((p, uid, gid)))
    ctx = _purge_ctx(tmp_path, lambda m: None)
    with pytest.raises(workflow.Blocked, match="re-run with --confirm"):
        workflow.purge(ctx, "/not/the/data/dir")
    assert claimed == [] and not ctx.compose.downed and ctx.paths.data.exists()
