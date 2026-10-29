import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(REPO.glob("compose*.yaml"))
ALLOWED_BIND_ROOTS = (
    "${NEOPS_DATA_DIR:-./data}/",
    "./generated/",
    "./certs/",
    "./cms/",
    "./cust-cert/",
    "./metrics/",
    "./logs/",
    "${NEOPS_TLS_CERT_FILE:-./certs/cert.pem}",
    "${NEOPS_TLS_KEY_FILE:-./certs/key.pem}",
)
# A `${VAR:-default}` token contains its own colon, so a plain split(":") on the short volume
# syntax "SOURCE:TARGET[:MODE]" misidentifies the source; treat such a token as one field.
BIND_SOURCE_RE = re.compile(r"^(\$\{[^}]+\}[^:]*|[^:]+):")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def bind_source(v: str | dict) -> str:
    if not isinstance(v, str):
        return v.get("source", "")
    match = BIND_SOURCE_RE.match(v)
    return match.group(1) if match else v


def is_allowed(src: str) -> bool:
    return any(src == root.rstrip("/") or src.startswith(root) for root in ALLOWED_BIND_ROOTS)


def test_no_named_volumes_anywhere():
    for f in COMPOSE_FILES:
        doc = load(f)
        assert "volumes" not in doc, f"{f.name} declares top-level volumes"
        for name, svc in (doc.get("services") or {}).items():
            for v in svc.get("volumes") or []:
                src = bind_source(v)
                assert is_allowed(src), f"{f.name}: service {name} mounts {src!r} outside the bind roots"


def test_base_stack_has_the_expected_services():
    doc = load(REPO / "compose.yaml")
    assert set(doc["services"]) == {
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
    }
    for name, svc in doc["services"].items():
        assert "ports" not in svc, f"base file must publish nothing, {name} does"
        assert svc.get("logging", {}).get("driver") == "json-file", f"{name} lacks the logging block"


def test_the_celery_containers_are_off_unless_the_profile_is_active():
    """A 2.0 deployment automates through the engine and the worker SDK; the 1.0 task path
    (Celery worker and beat) starts only with COMPOSE_PROFILES=cms-tasks."""
    services = load(REPO / "compose.yaml")["services"]
    for name, svc in services.items():
        expected = ["cms-tasks"] if name in ("cms-worker", "cms-beat") else None
        assert svc.get("profiles") == expected, name


def test_no_interpolation_of_generated_only_keys():
    generated_only = {
        "DJANGO_ALLOWED_HOSTS",
        "CORS_ORIGIN_ALLOW_ALL",
        "ACCOUNT_DEFAULT_HTTP_PROTOCOL",
        "KC_HTTP_RELATIVE_PATH",
        "SESSION_COOKIE_SECURE",
        "CSRF_COOKIE_SECURE",
        "MONITOR_BASE_PATH",
        "GF_PLUGINS_DISABLE_PLUGINS",
    }
    for f in COMPOSE_FILES:
        for key in generated_only:
            assert not re.search(r"\$\{?" + key, f.read_text()), (
                f"{f.name} interpolates {key}, which only exists in generated/"
            )


def test_no_shipped_compose_file_is_the_placeholder():
    for f in COMPOSE_FILES:
        doc = load(f)
        assert doc.get("services"), f"{f.name} is still the placeholder"


def test_admin_credentials_are_confined_to_cms_init():
    doc = load(REPO / "compose.yaml")
    services = doc["services"]
    assert "NEOPS_ADMIN_PASSWORD" in services["cms-init"]["environment"]
    for name in ("cms", "cms-worker", "cms-beat"):
        assert "NEOPS_ADMIN_PASSWORD" not in services[name]["environment"], (
            f"{name} must not carry NEOPS_ADMIN_PASSWORD, only cms-init needs it"
        )


def test_cms_init_seeds_the_admin_role():
    """A Django superuser holding no Neops role can log in and then read and write nothing, so
    the role seed belongs to the install rather than to an operator's first manual step."""
    init = load(REPO / "compose.yaml")["services"]["cms-init"]
    assert (REPO / "cms" / "bootstrap_admin_role.py").is_file()
    sources = [bind_source(v) for v in init["volumes"]]
    assert "./cms/bootstrap_admin_role.py" in sources
    # cms-init overrides the anchor's volume list rather than extending it, so the mounts the
    # anchor carries have to be repeated here or they silently disappear.
    assert "${NEOPS_DATA_DIR:-./data}/secrets/jwt" in sources
    command = " ".join(init["command"])
    assert "/etc/neops/bootstrap_admin_role.py" in command
    assert "grant_workflow_permissions" in command
    assert "NEOPS_ADMIN_ROLE" in init["environment"]


def test_compose_documents_the_same_admin_user_the_cli_falls_back_to():
    """cms-init creates that account and the CLI logs in as it: two defaults drifting apart
    means doctor and `rotate admin-password` address a user the install never made."""
    from neops_compose.context import DEFAULT_ADMIN_USER

    init = load(REPO / "compose.yaml")["services"]["cms-init"]
    assert init["environment"]["NEOPS_ADMIN_USER"] == f"${{NEOPS_ADMIN_USER:-{DEFAULT_ADMIN_USER}}}"
    assert f"NEOPS_ADMIN_USER={DEFAULT_ADMIN_USER}\n" in (REPO / ".env.example").read_text()


def test_disk_preflight_predicts_the_watermarks_the_container_actually_gets():
    """`./neops check` predicts the free space Elasticsearch demands before it will allocate a
    shard. The prediction is worth having only while it agrees with the settings in the compose
    file, which the CLI cannot read at runtime (no YAML parser in its dependencies)."""
    from neops_compose.preflight import ES_DEFAULT_HEADROOM, ES_HIGH_WATERMARK, parse_es_size

    settings = dict(
        entry.split("=", 1)
        for entry in load(REPO / "compose.yaml")["services"]["elasticsearch"]["environment"]
    )
    watermark = settings["cluster.routing.allocation.disk.watermark.high"]
    assert ES_HIGH_WATERMARK == float(watermark.rstrip("%")) / 100

    headroom = settings["cluster.routing.allocation.disk.watermark.high.max_headroom"]
    default = VAR_RE.search(headroom)["default"]
    assert parse_es_size(default) == ES_DEFAULT_HEADROOM


def test_no_healthcheck_addresses_localhost():
    """localhost resolves to ::1 first in these images and the servers bind IPv4 only, so a
    healthcheck against it never passes (the monitor app's nginx is the one that bit us)."""
    for f in COMPOSE_FILES:
        for name, service in (load(f).get("services") or {}).items():
            test = (service.get("healthcheck") or {}).get("test")
            if not test:
                continue
            assert "localhost" not in " ".join(test), f"{f.name}: {name} healthcheck uses localhost"


def test_every_publicly_routed_service_has_a_healthcheck():
    """`up --wait` only waits for services that declare one, and doctor probes these
    through Traefik the moment `up` returns: without a healthcheck it races the boot."""
    from neops_compose.traefik_model import SERVICE_URLS

    declared = {
        name: service for f in COMPOSE_FILES for name, service in (load(f).get("services") or {}).items()
    }
    for name in SERVICE_URLS:
        assert declared[name].get("healthcheck"), f"{name} is routed publicly but has no healthcheck"


VAR_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")


INNERMOST_VAR_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:-|:\+)(?P<word>[^${}]*))?\}")


def _substitute(m: re.Match, env) -> str:
    value = env.get(m["name"], "")
    if m["op"] == ":+":
        return m["word"] if value else ""
    return value or (m["word"] or "")


def _resolve(text: str, env) -> str:
    """Compose interpolation as these files use it: ${X}, ${X:-default} and ${X:+word}, nested."""
    while (resolved := INNERMOST_VAR_RE.sub(lambda m: _substitute(m, env), text)) != text:
        text = resolved
    return text


def _binding(mapping: str, env) -> tuple[str, int]:
    """A published port is `[ADDRESS:]HOST:CONTAINER`; an address-less mapping binds 0.0.0.0."""
    fields = _resolve(mapping, env).split(":")
    address = fields[0] if len(fields) == 3 else "0.0.0.0"
    return address, int(fields[-2])


def _published_ports(env, scenario) -> list[tuple[str, int]]:
    """The tracked files' port mappings plus the fragment the shared-host overlay extends. A
    mapping two files repeat for one service is one port: Compose merges it."""
    from neops_compose.render import traefik_ports

    docs = [load(REPO / name) for name in scenario.files]
    if scenario.shared_host:
        docs.append(traefik_ports(env, scenario))
    mappings = {
        (name, mapping)
        for doc in docs
        for name, service in (doc.get("services") or {}).items()
        for mapping in service.get("ports") or []
    }
    return sorted(_binding(mapping, env) for _, mapping in mappings)


def test_the_merged_config_publishes_exactly_the_ports_preflight_reserves():
    """`./neops check` reserves the ports a scenario needs; anything else the stack binds is a
    collision nobody was warned about. Plain http has no websecure entrypoint, so no 443."""
    from neops_compose.env import Env
    from neops_compose.preflight import required_ports
    from neops_compose.scenario import Scenario

    for example in sorted(REPO.glob("examples/*.env")):
        env = Env(example)
        scenario = Scenario.from_env(env)
        assert _published_ports(env, scenario) == sorted(required_ports(env, scenario)), example.name


def test_the_shared_host_overlay_extends_the_fragment_render_writes():
    """The overlay names the file and render writes it: the two must agree or every
    shared-host `docker compose config` fails with a missing file."""
    from neops_compose.render import TRAEFIK_PORTS_FRAGMENT

    traefik = load(REPO / "compose.traefik-shared-host.yaml")["services"]["traefik"]
    assert traefik == {"extends": {"file": f"./generated/{TRAEFIK_PORTS_FRAGMENT}", "service": "traefik"}}


def test_generated_env_files_added_after_the_first_release_are_optional():
    """`./neops up` on an existing deployment loads the compose config (downgrade guard) before
    `render` writes generated/. A required env file that the previous release never rendered
    makes that load fail, so only the files rendered since the first release may be required."""
    rendered_since_first_release = {"./generated/cms.env", "./generated/keycloak.env"}
    for f in COMPOSE_FILES:
        for name, svc in (load(f).get("services") or {}).items():
            for entry in svc.get("env_file") or []:
                path = entry["path"] if isinstance(entry, dict) else entry
                if not str(path).startswith("./generated/") or path in rendered_since_first_release:
                    continue
                assert isinstance(entry, dict) and entry.get("required") is False, f"{f.name}: {name} {path}"


DATA_ROOT = "${NEOPS_DATA_DIR:-./data}/"


def test_every_data_bind_mount_sits_in_a_directory_the_migrations_create():
    """Docker creates a missing bind source as root, which no non-root image can then write."""
    from neops_compose.env import Env
    from neops_compose.paths import Paths

    paths = Paths.for_repo(Path("/repo"), Env(Path("/nonexistent")))
    created = {d.relative_to(paths.data) for d in paths.data_dirs()}
    for f in COMPOSE_FILES:
        for name, svc in (load(f).get("services") or {}).items():
            for v in svc.get("volumes") or []:
                src = bind_source(v)
                if not src.startswith(DATA_ROOT):
                    continue
                rel = Path(src.removeprefix(DATA_ROOT))
                assert rel in created or rel.parent in created, f"{f.name}: {name} mounts {src}"


def test_grafana_alone_depends_on_nothing_and_provisions_only_the_rendered_log_datasource():
    """A logs-only file list must load, and a dependency on victoriametrics, which only the
    metrics overlay defines, fails `docker compose config`. The metrics overlay mounts its own
    datasource; the log datasource follows the backend, so ./neops render writes it."""
    grafana = load(REPO / "compose.grafana.yaml")["services"]["grafana"]
    assert "depends_on" not in grafana
    provisioned = [v for v in grafana["volumes"] if "/etc/grafana/provisioning" in v]
    assert provisioned == [
        "./generated/grafana/log-datasources.yml:/etc/grafana/provisioning/datasources/neops-logs.yml:ro"
    ]


def test_only_render_decides_which_grafana_apps_run():
    for f in COMPOSE_FILES:
        assert "GF_PLUGINS_DISABLE_PLUGINS" not in f.read_text(), f.name


def test_the_worker_is_never_put_on_the_log_store_network():
    assert "networks" not in load(REPO / "compose.yaml")["services"]["worker"]
    for f in COMPOSE_FILES:
        assert "worker" not in (load(f).get("services") or {}) or f.name == "compose.yaml", f.name


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


needs_docker = pytest.mark.skipif(not _docker_compose_available(), reason="docker compose is not installed")
EXAMPLE = (REPO / "examples" / "metrics-logs.env").read_text()
FILES_LINE = re.compile(r"^COMPOSE_FILE=.*$", re.MULTILINE)
TRAEFIK_BASE = "compose.yaml:compose.traefik.yaml:compose.tls-files.yaml"


def merged(tmp_path: Path, overlays: str) -> dict:
    """The project `docker compose config` resolves for the shared example with these overlays,
    the cms-tasks profile on, and the scratch directory written as <repo>."""
    from tests.compose_config_check import compose_config, prepare

    scratch = tmp_path / overlays.replace(":", "+")
    scratch.mkdir()
    env_text = FILES_LINE.sub(f"COMPOSE_FILE={TRAEFIK_BASE}:{overlays}", EXAMPLE)
    prepare(scratch, env_text + "COMPOSE_PROFILES=cms-tasks\n")
    result = compose_config(scratch, "--format", "json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.replace(str(scratch), "<repo>"))


@needs_docker
@pytest.mark.parametrize(
    "overlays",
    [
        "compose.metrics.yaml",
        "compose.grafana.yaml:compose.metrics.yaml",
        "compose.grafana.yaml:compose.logs.yaml",
        "compose.grafana.yaml:compose.metrics.yaml:compose.logs.yaml",
    ],
    ids=["previous-release-metrics", "metrics", "logs", "metrics-logs"],
)
def test_every_overlay_combination_the_rules_allow_or_warn_about_loads(tmp_path, overlays):
    assert merged(tmp_path, overlays)["services"]["grafana"]["image"] == "grafana/grafana:12.4.9"


@needs_docker
def test_the_metrics_overlays_grafana_is_the_one_the_grafana_overlay_builds(tmp_path):
    """The previous release's .env loads compose.metrics.yaml without compose.grafana.yaml
    until migration 0003 runs: its copy of the grafana block must be faithful."""
    alone = merged(tmp_path, "compose.metrics.yaml")["services"]["grafana"]
    split = merged(tmp_path, "compose.grafana.yaml:compose.metrics.yaml")["services"]["grafana"]
    assert alone == split


@needs_docker
def test_the_log_store_is_reachable_from_the_engine_and_core_only(tmp_path):
    """Function-block code runs in the worker: on the store's network it could write lines for
    any run, bypassing the engine's lease, and read every line, bypassing its permission."""
    services = merged(tmp_path, "compose.grafana.yaml:compose.metrics.yaml:compose.logs.yaml")["services"]
    assert set(services["victorialogs"]["networks"]) == {"logs"}
    assert set(services["worker"]["networks"]) == {"default"}
    wired = [name for name, svc in services.items() if (svc.get("environment") or {}).get("NEOPS_LOG_URL")]
    assert sorted(wired) == ["cms", "cms-beat", "cms-init", "cms-worker", "engine"]
    for name in [*wired, "grafana"]:
        assert set(services[name]["networks"]) == {"default", "logs"}, name


@needs_docker
def test_no_grafana_mount_lands_inside_a_read_only_directory_mount(tmp_path):
    """Docker cannot create a mountpoint inside a read-only bind mount, so Grafana would not
    start with both overlays if one mounted a whole provisioning directory the other adds to."""
    services = merged(tmp_path, "compose.grafana.yaml:compose.metrics.yaml:compose.logs.yaml")["services"]
    volumes = services["grafana"]["volumes"]
    read_only = [v["target"] for v in volumes if v.get("read_only")]
    for v in volumes:
        nested = [ro for ro in read_only if v["target"].startswith(ro + "/")]
        assert not nested, f"{v['target']} is inside {nested}"


def test_no_compose_file_mounts_the_cms_metrics_route():
    """The CMS's /metrics discloses what the log store does; until the exporter has a network
    the worker cannot reach, compose gives the route no credential, so the CMS never mounts it."""
    for f in COMPOSE_FILES:
        for key in ("NEOPS_METRICS_TOKEN", "NEOPS_METRICS_ALLOW_ANONYMOUS"):
            assert key not in f.read_text(), f"{f.name} sets {key}"
