import re
from pathlib import Path

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


def _resolve(text: str, env) -> str:
    return VAR_RE.sub(lambda m: env.get(m["name"], m["default"] or ""), text)


def _binding(mapping: str, env) -> tuple[str, int]:
    """A published port is `[ADDRESS:]HOST:CONTAINER`; an address-less mapping binds 0.0.0.0."""
    fields = _resolve(mapping, env).split(":")
    address = fields[0] if len(fields) == 3 else "0.0.0.0"
    return address, int(fields[-2])


def _published_ports(env, scenario) -> list[tuple[str, int]]:
    """The tracked files' port mappings plus the fragment the shared-host overlay extends."""
    from neops_compose.render import traefik_ports

    docs = [load(REPO / name) for name in scenario.files]
    if scenario.shared_host:
        docs.append(traefik_ports(env, scenario))
    return sorted(
        _binding(mapping, env)
        for doc in docs
        for service in (doc.get("services") or {}).values()
        for mapping in service.get("ports") or []
    )


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
