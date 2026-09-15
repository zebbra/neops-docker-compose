import pytest

from neops_compose import workflow
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
        self.calls.append("up")


def make_ctx(tmp_path) -> Ctx:
    (tmp_path / ".env").write_text("COMPOSE_FILE=compose.yaml\n")
    env = Env(tmp_path / ".env")
    return Ctx(
        repo=tmp_path,
        env=env,
        paths=Paths.for_repo(tmp_path, env),
        scenario=Scenario.from_env(env),
        compose=FakeCompose(),
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
