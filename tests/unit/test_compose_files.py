import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(REPO.glob("compose*.yaml"))
ALLOWED_BIND_ROOTS = (
    "${NEOPS_DATA_DIR:-./data}/",
    "./generated/",
    "./certs/",
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


def test_no_interpolation_of_generated_only_keys():
    generated_only = {
        "DJANGO_ALLOWED_HOSTS",
        "CORS_ORIGIN_ALLOW_ALL",
        "ACCOUNT_DEFAULT_HTTP_PROTOCOL",
        "KC_HTTP_RELATIVE_PATH",
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
