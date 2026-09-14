import pytest

from neops_compose import preflight
from neops_compose.compose import ComposeError
from neops_compose.env import Env
from neops_compose.scenario import Scenario
from tests.unit.test_rules import GOOD


def test_compose_version_parsing():
    assert preflight.version_ok("v2.24.1", (2, 24)) is True
    assert preflight.version_ok("2.20.3", (2, 24)) is False
    assert preflight.version_ok("5.4.0", (2, 24)) is True


def test_port_free_detects_a_bound_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert preflight.port_free("127.0.0.1", port) is False
    finally:
        s.close()
    assert preflight.port_free("127.0.0.1", port) is True


def test_required_ports_by_scenario(tmp_path):
    (tmp_path / ".env").write_text(
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.traefik-shared-host.yaml:"
        "compose.tls-files.yaml\nNEOPS_HTTP_PORT=8880\n"
    )
    env = Env(tmp_path / ".env")
    assert preflight.required_ports(env, Scenario.from_env(env)) == [
        ("0.0.0.0", 8880),
        ("0.0.0.0", 443),
        ("0.0.0.0", 8443),
    ]
    (tmp_path / ".env").write_text("COMPOSE_FILE=compose.yaml:compose.expose.yaml\nNEOPS_CMS_PORT=9000\n")
    env = Env(tmp_path / ".env")
    assert preflight.required_ports(env, Scenario.from_env(env)) == [
        ("127.0.0.1", 8080),
        ("127.0.0.1", 9000),
        ("127.0.0.1", 3030),
        ("127.0.0.1", 3031),
    ]


def test_report_format():
    checks = [preflight.Check("docker", True, "29.7.2"), preflight.Check("ports", False, "443 is in use")]
    text = preflight.format_report(checks)
    assert "OK   docker" in text and "FAIL ports" in text and "443 is in use" in text
    assert preflight.all_ok(checks) is False


class FakeCompose:
    def __init__(self, running=(), images=("quay.io/zebbra/neops-core:2.1.0-beta.5",), fail_images=False):
        self._running = set(running)
        self._images = list(images)
        self.fail_images = fail_images
        self.execs = []

    def running_services(self):
        return self._running

    def images(self):
        if self.fail_images:
            raise ComposeError(
                "docker compose config --images failed (1) env file generated/cms.env not found"
            )
        return self._images

    def exec(self, service, *cmd, env=None):
        self.execs.append((service, cmd, env))
        return ""


def fake_cmd(results):
    """Map a command's first two words to (returncode, output); anything unlisted succeeds."""

    def run(*args):
        return results.get(" ".join(args[:2]), (0, "ok"))

    return run


def detail_for(checks, name):
    return [c.detail for c in checks if c.name == name]


def prepared(tmp_repo, monkeypatch, results, **compose_kwargs):
    (tmp_repo / ".env").write_text(GOOD)
    env = Env(tmp_repo / ".env")
    monkeypatch.setattr(preflight, "_cmd", fake_cmd(results))
    compose = FakeCompose(**compose_kwargs)
    checks = preflight.run_checks(
        env, Scenario.from_env(env), tmp_repo, tmp_repo / "data", compose, check_images=True
    )
    return checks, compose


def test_run_checks_with_fake_compose(tmp_repo, monkeypatch):
    checks, compose = prepared(tmp_repo, monkeypatch, {"docker compose": (0, "5.4.0")})
    assert detail_for(checks, ".env") == ["3 compose files, scenario valid"]
    assert detail_for(checks, "image") == ["quay.io/zebbra/neops-core:2.1.0-beta.5 (local)"]
    assert [c.name for c in checks if c.name == "ports"], "a stopped stack checks its ports"


def test_run_checks_reports_a_broken_compose_config_instead_of_raising(tmp_repo, monkeypatch):
    checks, _ = prepared(tmp_repo, monkeypatch, {"docker compose": (0, "5.4.0")}, fail_images=True)
    image = [c for c in checks if c.name == "image"]
    assert len(image) == 1 and image[0].ok is False
    assert "run ./neops render first" in image[0].detail


def test_run_checks_skips_docker_work_when_the_daemon_is_down(tmp_repo, monkeypatch):
    results = {"docker version": (1, "Cannot connect to the Docker daemon"), "docker compose": (0, "5.4.0")}
    checks, compose = prepared(tmp_repo, monkeypatch, results)
    daemon = [c for c in checks if c.name == "docker daemon"]
    assert daemon[0].ok is False and "not running or not reachable" in daemon[0].detail
    assert detail_for(checks, "image") == [], "no image checks without a daemon"
    assert compose.execs == [], "no db password probes without a daemon"
    assert preflight.all_ok(checks) is False


def test_running_stack_skips_the_port_checks_visibly(tmp_repo, monkeypatch):
    checks, _ = prepared(
        tmp_repo, monkeypatch, {"docker compose": (0, "5.4.0")}, running=("cms", "postgres-cms")
    )
    ports = [c for c in checks if c.name == "ports"]
    assert len(ports) == 1 and ports[0].ok is True
    assert ports[0].detail == "skipped: the stack is running"


def test_db_password_is_probed_only_for_running_services(tmp_repo, monkeypatch):
    checks, compose = prepared(
        tmp_repo, monkeypatch, {"docker compose": (0, "5.4.0")}, running=("cms", "postgres-cms")
    )
    assert [service for service, _, _ in compose.execs] == ["postgres-cms"]
    assert compose.execs[0][2] == {"PGPASSWORD": "a1b2c3d4e5f6a1b2c3d4e5f6"}
    assert detail_for(checks, "db password") == ["postgres-cms accepts NEOPS_CMS_DB_PASSWORD"]


def test_uncached_image_falls_back_to_the_registry(monkeypatch):
    seen = []

    def run(*args):
        seen.append(args[:3])
        return (1, "no such image") if args[1] == "image" else (0, "{}")

    monkeypatch.setattr(preflight, "_cmd", run)
    check = preflight._image_check("quay.io/zebbra/neops-core:2.1.0-beta.5")
    assert check.ok is True and check.detail == "quay.io/zebbra/neops-core:2.1.0-beta.5"
    assert [a[1] for a in seen] == ["image", "manifest"]


def test_unpullable_image_names_the_registry_error(monkeypatch):
    monkeypatch.setattr(preflight, "_cmd", lambda *args: (1, "denied\nno such manifest: quay.io/x:1"))
    check = preflight._image_check("quay.io/x:1")
    assert check.ok is False
    assert "no such manifest: quay.io/x:1" in check.detail and "docker login quay.io" in check.detail


def test_image_checks_keep_input_order(monkeypatch):
    monkeypatch.setattr(preflight, "_cmd", lambda *args: (0, "ok"))
    images = [f"img{n}:1" for n in range(12)]
    checks = preflight._image_checks(FakeCompose(images=images))
    assert [c.detail for c in checks] == [f"{i} (local)" for i in images]


@pytest.mark.parametrize("missing_env", [True, False])
def test_missing_env_short_circuits_before_any_docker_work(tmp_path, monkeypatch, missing_env):
    if not missing_env:
        (tmp_path / ".env").write_text(GOOD)
    env = Env(tmp_path / ".env")
    monkeypatch.setattr(preflight, "_cmd", fake_cmd({"docker compose": (0, "5.4.0")}))
    compose = FakeCompose()
    checks = preflight.run_checks(env, Scenario.from_env(env), tmp_path, tmp_path / "data", compose)
    assert (detail_for(checks, ".env") == ["no .env file: copy one of examples/*.env to .env"]) is missing_env
