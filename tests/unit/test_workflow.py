import os

import pytest

from neops_compose import secrets, workflow
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
