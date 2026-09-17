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


def ports_for(tmp_path, text: str) -> list[tuple[str, int]]:
    (tmp_path / ".env").write_text(text)
    env = Env(tmp_path / ".env")
    return preflight.required_ports(env, Scenario.from_env(env))


def test_required_ports_by_scenario(tmp_path):
    traefik = ports_for(
        tmp_path,
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.traefik-shared-host.yaml:"
        "compose.tls-files.yaml\nNEOPS_HTTP_PORT=8880\n",
    )
    assert traefik == [("0.0.0.0", 8880), ("0.0.0.0", 443), ("0.0.0.0", 8443)]

    expose = ports_for(tmp_path, "COMPOSE_FILE=compose.yaml:compose.expose.yaml\nNEOPS_CMS_PORT=9000\n")
    assert expose == [
        ("127.0.0.1", 8080),
        ("127.0.0.1", 9000),
        ("127.0.0.1", 3030),
        ("127.0.0.1", 3031),
    ]


SHARED = (
    "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.traefik-shared-host.yaml:compose.tls-files.yaml\n"
    "NEOPS_WEB_URL=https://neops.example.com\n"
    "NEOPS_CMS_URL=https://neops.example.com\n"
    "NEOPS_ENGINE_URL=https://neops.example.com/engine\n"
)


@pytest.mark.parametrize(
    "workflows_url,reserved",
    [
        ("https://neops.example.com:8443", True),
        ("https://neops.example.com/workflows", False),
        ("https://workflows.neops.example.com", False),
    ],
)
def test_the_monitor_port_is_reserved_only_for_a_monitor_on_its_own_entrypoint(
    tmp_path, workflows_url, reserved
):
    ports = ports_for(tmp_path, SHARED + f"NEOPS_WORKFLOWS_URL={workflows_url}\n")
    assert (("0.0.0.0", 8443) in ports) is reserved


def test_a_rejected_workflows_url_still_reserves_the_monitor_port(tmp_path):
    """The .env check reports the URL; the port check reports a collision on top, if any."""
    ports = ports_for(tmp_path, SHARED + "NEOPS_WORKFLOWS_URL=not a url\n")
    assert ("0.0.0.0", 8443) in ports


def test_the_keycloak_and_grafana_loopback_ports_are_reserved_in_both_proxy_modes(tmp_path):
    """Both overlays publish their port unconditionally, so Traefik in front of them changes
    nothing: an unreserved port is a collision the operator was never warned about."""
    overlays = "compose.keycloak.yaml:compose.oidc.yaml:compose.metrics.yaml"
    behind_traefik = ports_for(tmp_path, f"COMPOSE_FILE=compose.yaml:compose.traefik.yaml:{overlays}\n")
    assert behind_traefik == [("0.0.0.0", 80), ("127.0.0.1", 8180), ("127.0.0.1", 3000)]

    expose = ports_for(
        tmp_path,
        f"COMPOSE_FILE=compose.yaml:compose.expose.yaml:{overlays}\n"
        "NEOPS_BIND_ADDRESS=10.0.0.5\nNEOPS_KEYCLOAK_PORT=18180\nNEOPS_GRAFANA_PORT=13000\n",
    )
    assert expose[-2:] == [("10.0.0.5", 18180), ("10.0.0.5", 13000)]
    assert all(address == "10.0.0.5" for address, _ in expose)


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
    checks = preflight.image_checks(FakeCompose(images=images))
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


def test_a_missing_docker_binary_fails_the_check_instead_of_raising(monkeypatch, tmp_path):
    def missing(args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr("neops_compose.preflight.subprocess.run", missing)
    checks, daemon_ok, compose_ok = preflight._docker_checks()
    assert not daemon_ok and not compose_ok
    assert all(not c.ok for c in checks)
    assert all("not installed or not on PATH" in c.detail for c in checks)


GIB = 2**30


def env_with(tmp_path, text: str = "") -> Env:
    (tmp_path / ".env").write_text(text)
    return Env(tmp_path / ".env")


def test_parse_es_size():
    assert preflight.parse_es_size("5GB") == 5 * GIB
    assert preflight.parse_es_size("150gb") == 150 * GIB
    assert preflight.parse_es_size("512m") == 512 * 2**20
    assert preflight.parse_es_size("1T") == 2**40
    for bad in ("", "plenty", "5XB", "GB"):
        assert preflight.parse_es_size(bad) is None, bad


def test_es_free_space_needed_is_ten_percent_until_the_headroom_caps_it(tmp_path):
    """A percentage watermark makes the demand proportional to the filesystem, which says nothing
    about what this stack needs; NEOPS_ES_HEADROOM caps it at an absolute size."""
    total = 812 * GIB
    assert preflight.es_free_space_needed(env_with(tmp_path), total) == round(0.10 * total)
    capped = preflight.es_free_space_needed(env_with(tmp_path, "NEOPS_ES_HEADROOM=5GB\n"), total)
    assert capped == 5 * GIB


def test_es_free_space_needed_lets_the_percentage_bind_on_a_small_filesystem(tmp_path):
    """Below the cap it is the watermark, not NEOPS_ES_HEADROOM, that decides."""
    assert preflight.es_free_space_needed(env_with(tmp_path), 100 * GIB) == 10 * GIB


def test_es_free_space_needed_rejects_a_headroom_that_is_not_a_byte_size(tmp_path):
    assert preflight.es_free_space_needed(env_with(tmp_path, "NEOPS_ES_HEADROOM=plenty\n"), 812 * GIB) is None


def test_disk_check_fails_when_the_default_demand_is_unmet(tmp_path):
    """The failure that made cms-init time out: 22 GiB free on an 812 GiB filesystem, where the
    uncapped 90% watermark demands 81 GiB and Elasticsearch then refuses every shard."""
    check = preflight.disk_check(env_with(tmp_path), total=812 * GIB, free=22 * GIB)
    assert not check.ok
    assert check.name == "disk"
    assert "81.2 GiB" in check.detail and "22.0 GiB" in check.detail


def test_disk_check_passes_once_the_headroom_caps_the_demand(tmp_path):
    check = preflight.disk_check(
        env_with(tmp_path, "NEOPS_ES_HEADROOM=5GB\n"), total=812 * GIB, free=22 * GIB
    )
    assert check.ok
    assert "5.0 GiB" in check.detail


def test_disk_check_still_fails_under_the_capped_demand(tmp_path):
    check = preflight.disk_check(env_with(tmp_path, "NEOPS_ES_HEADROOM=5GB\n"), total=812 * GIB, free=2 * GIB)
    assert not check.ok
    assert "5.0 GiB" in check.detail and "2.0 GiB" in check.detail


def test_disk_check_names_a_headroom_it_cannot_parse(tmp_path):
    check = preflight.disk_check(env_with(tmp_path, "NEOPS_ES_HEADROOM=plenty\n"), 812 * GIB, 22 * GIB)
    assert not check.ok
    assert "NEOPS_ES_HEADROOM" in check.detail and "plenty" in check.detail
