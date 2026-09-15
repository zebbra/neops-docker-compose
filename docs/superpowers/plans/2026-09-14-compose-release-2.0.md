# neops-docker-compose 2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `release/2.0` branch of `neops-docker-compose`: a production compose deployment of the Neops 2.0 stack with a uv-managed `./neops` CLI (one-line install, deployment migrations, key/token handling, doctor, backup, rotation), overlay-selected scenarios (external proxy, Traefik with three TLS modes, shared-hostname routing, OIDC with external IdP or bundled Keycloak, metrics), bind-mount-only durable state, and a fix to the engine's release workflow so the monitor image gets published.

**Architecture:** Static `compose.yaml` plus small overlays selected by `COMPOSE_FILE` in `.env`. A Python package `neops_compose` (argparse CLI) validates `.env`, renders the few files that depend on it (`generated/`: Traefik static+dynamic config, per-service env files, OIDC provider seed, Keycloak realm), generates key material under `data/secrets/`, runs numbered deployment migrations recorded in `data/.neops/state.json`, and orchestrates the two-phase first start (CMS up → mint engine API key → everything). Every durable byte lives under `data/` as a bind mount.

**Tech Stack:** Docker Compose v2 (≥ 2.24; 5.4 on the dev box), Python 3.12 via `uv`, `cryptography`, `python-dotenv`, `pytest`, `ruff`; Traefik v3.6.25, Keycloak 26.5.2, Postgres 16, Redis 7, Elasticsearch 8.9.2; Neops images pinned in `compose.yaml`.

**Spec:** `docs/superpowers/specs/2026-09-14-compose-release-2.0-design.md` (approved). Read it first; this plan does not repeat its rationale.

**Working directory for every command below:** the repo root `neops-docker-compose/` (from the workspace root: `cd neops-docker-compose`). Branch `release/2.0`. Commit messages: no `Co-Authored-By` or AI trailers (user policy).

**Dev-box constraints:** ports 80/443 are taken by a KIND cluster; e2e runs use `NEOPS_HTTP_PORT=8880`, `NEOPS_HTTPS_PORT=8443`, `NEOPS_MONITOR_PORT=8444`. Quay is logged in; `quay.io/zebbra/neops-monitor-app` has no tag until Task 26 lands upstream, so e2e uses `NEOPS_MONITOR_IMAGE` pointing at a locally built image (Task 24). Bridge-network DNS on this box is unreliable for `docker build`; build with `--network=host`.

---

## File map

| Path | Responsibility |
|---|---|
| `neops` | bash wrapper → `uv run --project <repo> --quiet neops "$@"` |
| `pyproject.toml`, `uv.lock` | uv project `neops-compose`, script `neops = neops_compose.cli:main` |
| `neops_compose/__init__.py` | `__version__` |
| `neops_compose/paths.py` | `Paths`: repo, data, generated, secrets, backups, state file, certs |
| `neops_compose/env.py` | `Env`: read/write `.env`, `MissingEnv` |
| `neops_compose/urls.py` | `PublicUrl` parsing + validation |
| `neops_compose/scenario.py` | `Scenario` from `COMPOSE_FILE`, overlay flags |
| `neops_compose/rules.py` | `problems(env, scenario, repo) -> list[str]` (required keys, URL rules, overlay rules) |
| `neops_compose/routes.py` | core prefixes, engine denied worker routes, web reserved paths |
| `neops_compose/traefik_model.py` | `build_traefik(env, scenario) -> TraefikConfig` (entrypoints, routers, services, middlewares, tls) |
| `neops_compose/render.py` | write `generated/` (env files, provider seed, realm, Traefik config) from the models |
| `neops_compose/secrets.py` | JWT keypair, self-signed TLS, keycloak client secret |
| `neops_compose/compose.py` | `Compose` subprocess wrapper (cwd = repo) |
| `neops_compose/state.py` | `State` load/save (`data/.neops/state.json`) |
| `neops_compose/migrate.py` | discover/apply/fake migrations, `Ctx` |
| `neops_compose/token.py` | ensure/rotate the engine's CMS API key |
| `neops_compose/preflight.py` | `check` (host prerequisites, image manifests, env, db password) |
| `neops_compose/doctor.py` | health report |
| `neops_compose/backup.py` | logical backup archive |
| `neops_compose/rotate.py` | secret rotations |
| `neops_compose/workflow.py` | `install`, `up`, `purge` orchestration |
| `neops_compose/cli.py` | argparse → functions; `Ctx` construction |
| `migrations/0001_initial_layout.py` | data tree + state file |
| `compose.yaml` + 8 overlays | the stack |
| `examples/*.env`, `.env.example` | operator-facing configuration |
| `tests/unit/*` | pytest |
| `tests/e2e/run_scenario.py`, `tests/e2e/chaos.py` | end-to-end and chaos runs against real containers |
| `Makefile`, `.github/workflows/ci.yml` | `make check` = lint + test + compose-config |
| `README.md`, `docs/` | operator docs |

---

## Task 1: Clean the branch and scaffold the uv project

**Files:**
- Delete: `neops/`, `traefik/`, `keycloak/`, `secure-gw/`, `kea/`, `.support/`, `docker-compose.yaml`, `README.md`
- Keep: `metrics/` (ported in Task 12), `.gitattributes`
- Create: `pyproject.toml`, `neops`, `neops_compose/__init__.py`, `.gitignore`, `tests/unit/__init__.py`, `tests/unit/conftest.py`

- [ ] **Step 1: Remove the 1.0 layout**

```bash
git rm -rq neops traefik keycloak secure-gw kea .support docker-compose.yaml README.md
git commit -qm "chore: remove the 1.0 compose layout from release/2.0"
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "neops-compose"
version = "2.0.0"
description = "Operator CLI for the Neops 2.0 docker-compose deployment"
requires-python = ">=3.12"
dependencies = [
  "cryptography>=43",
  "python-dotenv>=1.0",
]

[project.scripts]
neops = "neops_compose.cli:main"

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6", "pyyaml>=6"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["neops_compose"]

[tool.ruff]
line-length = 110
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]

[tool.pytest.ini_options]
testpaths = ["tests/unit"]
```

- [ ] **Step 3: Write the wrapper `neops`**

```bash
#!/usr/bin/env bash
# Operator entrypoint. Needs only uv and docker on the host.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec uv run --project "$here" --quiet neops "$@"
```

Then `chmod +x neops`.

- [ ] **Step 4: Write `neops_compose/__init__.py`**

```python
__version__ = "2.0.0"
```

- [ ] **Step 5: Write `.gitignore`**

```
.env
compose.override.yaml
/generated/
/data/
/backups/
/certs/*
!/certs/.keep
/cust-cert/*
!/cust-cert/.keep
.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
*.egg-info/
site/
```

- [ ] **Step 6: Write `tests/unit/conftest.py`**

```python
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo() -> Path:
    return REPO


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A throwaway repo root with the real compose files and templates symlinked in."""
    (tmp_path / "migrations").symlink_to(REPO / "migrations")
    for f in REPO.glob("compose*.yaml"):
        (tmp_path / f.name).symlink_to(f)
    (tmp_path / "certs").mkdir()
    (tmp_path / "cust-cert").mkdir()
    return tmp_path
```

- [ ] **Step 7: Lock, verify the wrapper runs, commit**

```bash
mkdir -p certs cust-cert && touch certs/.keep cust-cert/.keep tests/unit/__init__.py
uv lock && uv sync
./neops --help 2>&1 | head -3   # expected: an ImportError about neops_compose.cli — cli.py comes in Task 20; the wrapper itself resolving uv is what this checks
git add -A && git commit -qm "chore: scaffold the uv project and the ./neops wrapper"
```

---

## Task 2: `paths.py` and `env.py`

**Files:**
- Create: `neops_compose/paths.py`, `neops_compose/env.py`
- Test: `tests/unit/test_env.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_env.py
from pathlib import Path

import pytest

from neops_compose.env import Env, MissingEnv
from neops_compose.paths import Paths


def write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_env_reads_values_and_treats_blank_as_unset(tmp_path):
    env = Env(write(tmp_path / ".env", "A=1\nB=\n# c\nC='quoted'\n"))
    assert env.get("A") == "1"
    assert env.get("B", "fallback") == "fallback"
    assert env.get("C") == "quoted"
    assert env.is_set("A") and not env.is_set("B") and not env.is_set("ZZ")


def test_env_require_raises_with_key_name(tmp_path):
    env = Env(write(tmp_path / ".env", "A=1\n"))
    with pytest.raises(MissingEnv, match="NEOPS_X"):
        env.require("NEOPS_X")


def test_env_set_rename_unset_preserve_comments(tmp_path):
    p = write(tmp_path / ".env", "# keep me\nOLD=1\nOTHER=2\n")
    env = Env(p)
    env.set("NEW", "x")
    env.rename("OLD", "RENAMED")
    env.unset("OTHER")
    text = p.read_text()
    assert "# keep me" in text
    assert "RENAMED=1" in text and "OLD=" not in text
    assert "NEW=x" in text and "OTHER" not in text
    assert Env(p).get("RENAMED") == "1"


def test_env_missing_file_is_empty(tmp_path):
    env = Env(tmp_path / ".env")
    assert env.values == {} and not env.exists


def test_paths_default_data_dir_is_under_repo(tmp_path):
    env = Env(write(tmp_path / ".env", ""))
    paths = Paths.for_repo(tmp_path, env)
    assert paths.data == tmp_path / "data"
    assert paths.secrets == tmp_path / "data" / "secrets"
    assert paths.state_file == tmp_path / "data" / ".neops" / "state.json"
    assert paths.generated == tmp_path / "generated"


def test_paths_honour_neops_data_dir_relative_to_repo(tmp_path):
    env = Env(write(tmp_path / ".env", "NEOPS_DATA_DIR=/srv/neops\n"))
    assert Paths.for_repo(tmp_path, env).data == Path("/srv/neops")
    env = Env(write(tmp_path / ".env", "NEOPS_DATA_DIR=./elsewhere\n"))
    assert Paths.for_repo(tmp_path, env).data == tmp_path / "elsewhere"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_env.py -q`
Expected: `ImportError` / collection error (modules do not exist).

- [ ] **Step 3: Write `neops_compose/env.py`**

```python
from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key


class MissingEnv(Exception):
    def __init__(self, key: str, hint: str = ""):
        super().__init__(f"{key} is not set in .env" + (f" ({hint})" if hint else ""))
        self.key = key


class Env:
    """The operator's .env file. Blank values count as unset, as they do for docker compose."""

    def __init__(self, path: Path):
        self.path = path
        self.exists = path.is_file()
        raw = dotenv_values(path) if self.exists else {}
        self.values: dict[str, str] = {k: (v or "") for k, v in raw.items()}

    def get(self, key: str, default: str = "") -> str:
        value = self.values.get(key, "")
        return value if value != "" else default

    def is_set(self, key: str) -> bool:
        return self.values.get(key, "") != ""

    def require(self, key: str, hint: str = "") -> str:
        if not self.is_set(key):
            raise MissingEnv(key, hint)
        return self.values[key]

    def flag(self, key: str) -> bool:
        return self.get(key).strip().lower() in {"1", "true", "yes", "on"}

    def set(self, key: str, value: str) -> None:
        self.path.touch(exist_ok=True)
        set_key(str(self.path), key, value, quote_mode="never")
        self.values[key] = value
        self.exists = True

    def unset(self, key: str) -> None:
        if key in self.values:
            unset_key(str(self.path), key)
            del self.values[key]

    def rename(self, old: str, new: str) -> None:
        if old not in self.values:
            return
        value = self.values[old]
        text = self.path.read_text()
        self.path.write_text(text.replace(f"\n{old}=", f"\n{new}=", 1) if not text.startswith(f"{old}=")
                             else text.replace(f"{old}=", f"{new}=", 1))
        self.values[new] = value
        del self.values[old]
```

- [ ] **Step 4: Write `neops_compose/paths.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from neops_compose.env import Env


@dataclass(frozen=True)
class Paths:
    repo: Path
    data: Path

    @classmethod
    def for_repo(cls, repo: Path, env: Env) -> Paths:
        raw = env.get("NEOPS_DATA_DIR", "./data")
        data = Path(raw)
        if not data.is_absolute():
            data = (repo / data).resolve()
        return cls(repo=repo.resolve(), data=data)

    @property
    def env_file(self) -> Path:
        return self.repo / ".env"

    @property
    def generated(self) -> Path:
        return self.repo / "generated"

    @property
    def backups(self) -> Path:
        return self.repo / "backups"

    @property
    def certs(self) -> Path:
        return self.repo / "certs"

    @property
    def migrations(self) -> Path:
        return self.repo / "migrations"

    @property
    def secrets(self) -> Path:
        return self.data / "secrets"

    @property
    def jwt_dir(self) -> Path:
        return self.secrets / "jwt"

    @property
    def engine_env(self) -> Path:
        return self.secrets / "engine.env"

    @property
    def keycloak_client_env(self) -> Path:
        return self.secrets / "keycloak-client.env"

    @property
    def tls_dir(self) -> Path:
        return self.secrets / "tls"

    @property
    def state_file(self) -> Path:
        return self.data / ".neops" / "state.json"

    def data_dirs(self) -> list[Path]:
        """Every bind-mounted directory the base stack and the overlays use."""
        return [
            self.data / "cms" / "postgres", self.data / "cms" / "media", self.data / "cms" / "tmp",
            self.data / "engine" / "postgres", self.data / "keycloak" / "postgres",
            self.data / "elasticsearch", self.data / "traefik" / "acme",
            self.data / "metrics" / "victoria", self.data / "metrics" / "grafana",
            self.secrets, self.jwt_dir, self.tls_dir, self.state_file.parent,
        ]
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/unit/test_env.py -q`
Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
git add neops_compose/env.py neops_compose/paths.py tests/unit/test_env.py
git commit -qm "feat(cli): .env access and the data/generated path model"
```

---

## Task 3: `urls.py`

**Files:**
- Create: `neops_compose/urls.py`
- Test: `tests/unit/test_urls.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_urls.py
import pytest

from neops_compose.urls import BadUrl, PublicUrl


def test_parse_https_default_port():
    u = PublicUrl.parse("https://cms.neops.example.com")
    assert (u.scheme, u.host, u.port, u.path) == ("https", "cms.neops.example.com", 443, "")
    assert u.origin == "https://cms.neops.example.com"
    assert str(u) == "https://cms.neops.example.com"
    assert u.is_default_port


def test_parse_with_port_and_path_normalises_trailing_slash():
    u = PublicUrl.parse("http://neops.example.com:8880/engine/")
    assert (u.port, u.path) == (8880, "/engine")
    assert u.origin == "http://neops.example.com:8880"
    assert str(u) == "http://neops.example.com:8880/engine"
    assert not u.is_default_port


@pytest.mark.parametrize("raw", [
    "neops.example.com",                 # no scheme
    "ftp://neops.example.com",           # bad scheme
    "https://user:pw@neops.example.com", # userinfo
    "https://neops.example.com/?x=1",    # query
    "https://neops.example.com/#f",      # fragment
    "https://neops example.com",         # space
    'https://neops.example.com/a"b',     # quote (breaks the web client's injected JS)
    "https://",                          # no host
])
def test_parse_rejects(raw):
    with pytest.raises(BadUrl):
        PublicUrl.parse(raw)


def test_same_origin():
    a = PublicUrl.parse("https://neops.example.com")
    b = PublicUrl.parse("https://neops.example.com/engine")
    c = PublicUrl.parse("https://neops.example.com:8443")
    assert a.same_origin(b) and not a.same_origin(c)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_urls.py -q` → collection error.

- [ ] **Step 3: Write `neops_compose/urls.py`**

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# The monitor app rejects anything outside this set, and the web client writes
# these values unescaped into a JS literal: keep public URLs plain.
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_PATH_RE = re.compile(r"^(/[A-Za-z0-9._~-]+)*$")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class BadUrl(ValueError):
    pass


@dataclass(frozen=True)
class PublicUrl:
    scheme: str
    host: str
    port: int
    path: str  # "" or "/a/b", never a trailing slash

    @classmethod
    def parse(cls, raw: str) -> PublicUrl:
        parts = urlsplit(raw.strip())
        if parts.scheme not in _DEFAULT_PORTS:
            raise BadUrl(f"{raw!r}: scheme must be http or https")
        if parts.username or parts.password:
            raise BadUrl(f"{raw!r}: user info is not allowed")
        if parts.query or parts.fragment or raw.rstrip().endswith("?") or raw.rstrip().endswith("#"):
            raise BadUrl(f"{raw!r}: query strings and fragments are not allowed")
        host = parts.hostname or ""
        if not host or not _HOST_RE.match(host):
            raise BadUrl(f"{raw!r}: hostname is missing or contains disallowed characters")
        path = parts.path.rstrip("/")
        if path and not _PATH_RE.match(path):
            raise BadUrl(f"{raw!r}: path contains disallowed characters")
        try:
            port = parts.port or _DEFAULT_PORTS[parts.scheme]
        except ValueError as exc:
            raise BadUrl(f"{raw!r}: invalid port") from exc
        return cls(parts.scheme, host, port, path)

    @property
    def is_default_port(self) -> bool:
        return self.port == _DEFAULT_PORTS[self.scheme]

    @property
    def origin(self) -> str:
        suffix = "" if self.is_default_port else f":{self.port}"
        return f"{self.scheme}://{self.host}{suffix}"

    def same_origin(self, other: PublicUrl) -> bool:
        return self.origin == other.origin

    def __str__(self) -> str:
        return self.origin + self.path
```

- [ ] **Step 4: Run the tests** → `uv run pytest tests/unit/test_urls.py -q` → `11 passed`

- [ ] **Step 5: Commit**

```bash
git add neops_compose/urls.py tests/unit/test_urls.py
git commit -qm "feat(cli): public URL parsing with the charset the frontends tolerate"
```

---

## Task 4: `scenario.py`, `routes.py`, `rules.py`

**Files:**
- Create: `neops_compose/scenario.py`, `neops_compose/routes.py`, `neops_compose/rules.py`
- Test: `tests/unit/test_rules.py`

- [ ] **Step 1: Write `neops_compose/routes.py`** (no test: constants, but the compose-invariant test in Task 11 and the Traefik model test in Task 5 use them)

```python
"""Path knowledge shared by the routing overlays.

CORE_PREFIXES: root-absolute URL prefixes neops-core serves, from
neops-core/backend/neopsapp/urls.py plus the neops_webhook plugin. In
shared-hostname mode these are routed to the CMS on the web client's hostname.
Adding a URL prefix to core is a two-repo change: update this list too.

ENGINE_PUBLIC_WORKER_ROUTES: the engine's @Public() worker routes
(neops-workflow-engine src/resources/{blackboard,workers,function-blocks}/*.controller.ts).
They are unauthenticated by design and must never be reachable from outside the
compose network. All are POST. A new @Public() route in the engine is a two-repo change.

WEB_RESERVED_PATHS: paths the web client SPA owns on its origin.
"""

CORE_PREFIXES: tuple[str, ...] = (
    "/graphql",
    "/graphiql",
    "/admin",
    "/djstatic",
    "/.well-known",
    "/accounts",
    "/auth/oidc-login",
    "/auth/oidc-complete",
    "/auth/oidc-logout",
    "/webhook",
)

# (matcher, value) pairs; traefik_model turns them into a Traefik v3 rule and
# prepends the engine's public path prefix when it has one. All are POST.
ENGINE_PUBLIC_WORKER_ROUTES: tuple[tuple[str, str], ...] = (
    ("path", "/blackboard/job"),
    ("prefix", "/blackboard/job/"),
    ("path", "/workers/register"),
    ("regexp", "/workers/[^/]+/(ping|unregister)"),
    ("path", "/function-blocks/register"),
)

WEB_RESERVED_PATHS: tuple[str, ...] = ("/auth", "/login")
```

- [ ] **Step 2: Write `neops_compose/scenario.py`**

```python
from __future__ import annotations

from dataclasses import dataclass

from neops_compose.env import Env

BASE_FILE = "compose.yaml"
OVERLAYS: dict[str, str] = {
    "expose": "compose.expose.yaml",
    "traefik": "compose.traefik.yaml",
    "shared-host": "compose.traefik-shared-host.yaml",
    "tls-files": "compose.tls-files.yaml",
    "tls-acme": "compose.tls-acme.yaml",
    "oidc": "compose.oidc.yaml",
    "keycloak": "compose.keycloak.yaml",
    "metrics": "compose.metrics.yaml",
}
OVERRIDE_FILE = "compose.override.yaml"


@dataclass(frozen=True)
class Scenario:
    files: tuple[str, ...]

    @classmethod
    def from_env(cls, env: Env) -> Scenario:
        sep = env.get("COMPOSE_PATH_SEPARATOR", ":")
        raw = env.get("COMPOSE_FILE", BASE_FILE)
        return cls(tuple(f.strip() for f in raw.split(sep) if f.strip()))

    def has(self, overlay: str) -> bool:
        return OVERLAYS[overlay] in self.files

    @property
    def proxy(self) -> str | None:
        if self.has("traefik"):
            return "traefik"
        if self.has("expose"):
            return "expose"
        return None

    @property
    def tls(self) -> str | None:
        if self.has("tls-files"):
            return "files"
        if self.has("tls-acme"):
            return "acme"
        return None

    @property
    def shared_host(self) -> bool:
        return self.has("shared-host")

    @property
    def oidc(self) -> bool:
        return self.has("oidc")

    @property
    def keycloak(self) -> bool:
        return self.has("keycloak")

    @property
    def metrics(self) -> bool:
        return self.has("metrics")

    @property
    def unknown_files(self) -> tuple[str, ...]:
        known = {BASE_FILE, OVERRIDE_FILE, *OVERLAYS.values()}
        return tuple(f for f in self.files if f not in known)
```

- [ ] **Step 3: Write the failing tests for `rules.py`**

```python
# tests/unit/test_rules.py
from pathlib import Path

import pytest

from neops_compose.env import Env
from neops_compose.rules import problems
from neops_compose.scenario import Scenario

GOOD = """
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml
COMPOSE_PATH_SEPARATOR=:
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_CMS_DB_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f6
NEOPS_ENGINE_DB_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f7
DJANGO_SECRET_KEY=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6
NEOPS_ADMIN_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f8
NEOPS_TLS_CERT_FILE=./certs/cert.pem
NEOPS_TLS_KEY_FILE=./certs/key.pem
"""


def make(tmp_repo: Path, text: str) -> tuple[Env, Scenario]:
    (tmp_repo / ".env").write_text(text)
    env = Env(tmp_repo / ".env")
    return env, Scenario.from_env(env)


def test_good_env_has_no_problems(tmp_repo):
    env, sc = make(tmp_repo, GOOD)
    assert problems(env, sc, tmp_repo) == []


def test_missing_required_key_and_placeholder_secret(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace("NEOPS_ADMIN_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f8", "NEOPS_ADMIN_PASSWORD=changeme")
                   .replace("NEOPS_CMS_DB_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f6\n", ""))
    out = problems(env, sc, tmp_repo)
    assert any("NEOPS_CMS_DB_PASSWORD" in p for p in out)
    assert any("NEOPS_ADMIN_PASSWORD" in p and "placeholder" in p for p in out)


def test_cms_url_with_path_is_rejected(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace("https://cms.neops.example.com", "https://neops.example.com/cms"))
    assert any("NEOPS_CMS_URL" in p and "path" in p for p in problems(env, sc, tmp_repo))


def test_cms_on_web_origin_requires_shared_host_overlay(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace("https://cms.neops.example.com", "https://neops.example.com"))
    assert any("compose.traefik-shared-host.yaml" in p for p in problems(env, sc, tmp_repo))


def test_shared_host_accepts_engine_path_and_distinct_monitor_origin(tmp_repo):
    text = (GOOD.replace("compose.traefik.yaml:", "compose.traefik.yaml:compose.traefik-shared-host.yaml:")
            .replace("https://cms.neops.example.com", "https://neops.example.com")
            .replace("https://engine.neops.example.com", "https://neops.example.com/engine")
            .replace("https://workflows.neops.example.com", "https://neops.example.com:8443"))
    env, sc = make(tmp_repo, text)
    assert problems(env, sc, tmp_repo) == []


def test_monitor_must_not_share_web_origin(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace("https://workflows.neops.example.com", "https://neops.example.com/workflows"))
    assert any("NEOPS_WORKFLOWS_URL" in p and "origin" in p for p in problems(env, sc, tmp_repo))


def test_engine_path_must_not_collide_with_core_or_web_paths(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace("https://engine.neops.example.com", "https://neops.example.com/auth"))
    assert any("NEOPS_ENGINE_URL" in p and "reserved" in p for p in problems(env, sc, tmp_repo))


@pytest.mark.parametrize("compose_file,needle", [
    ("compose.yaml", "compose.expose.yaml or compose.traefik.yaml"),
    ("compose.yaml:compose.expose.yaml:compose.traefik.yaml", "not both"),
    ("compose.yaml:compose.expose.yaml:compose.tls-files.yaml", "compose.traefik.yaml"),
    ("compose.yaml:compose.traefik.yaml:compose.tls-files.yaml:compose.tls-acme.yaml", "one of"),
    ("compose.yaml:compose.traefik.yaml:compose.keycloak.yaml", "compose.oidc.yaml"),
    ("compose.yaml:compose.traefik.yaml:compose.nope.yaml", "does not exist"),
])
def test_overlay_combination_rules(tmp_repo, compose_file, needle):
    env, sc = make(tmp_repo, GOOD.replace("COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml",
                                          f"COMPOSE_FILE={compose_file}"))
    assert any(needle in p for p in problems(env, sc, tmp_repo)), problems(env, sc, tmp_repo)


def test_acme_requires_port_80(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace("compose.tls-files.yaml", "compose.tls-acme.yaml") + "NEOPS_ACME_EMAIL=ops@example.com\nNEOPS_HTTP_PORT=8880\n")
    assert any("NEOPS_HTTP_PORT" in p for p in problems(env, sc, tmp_repo))


def test_oidc_external_requires_provider_keys_but_keycloak_does_not(tmp_repo):
    base = GOOD.replace("compose.tls-files.yaml", "compose.tls-files.yaml:compose.oidc.yaml")
    env, sc = make(tmp_repo, base)
    assert any("NEOPS_OIDC_CLIENT_ID" in p for p in problems(env, sc, tmp_repo))
    kc = base.replace("compose.oidc.yaml", "compose.oidc.yaml:compose.keycloak.yaml") + \
        "NEOPS_KEYCLOAK_URL=https://auth.neops.example.com\nNEOPS_KEYCLOAK_ADMIN_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5f9\nNEOPS_KEYCLOAK_DB_PASSWORD=a1b2c3d4e5f6a1b2c3d4e5fa\n"
    env, sc = make(tmp_repo, kc)
    assert problems(env, sc, tmp_repo) == []
```

- [ ] **Step 4: Run to verify failure** → `uv run pytest tests/unit/test_rules.py -q` → collection error.

- [ ] **Step 5: Write `neops_compose/rules.py`**

```python
from __future__ import annotations

from pathlib import Path

from neops_compose.env import Env
from neops_compose.routes import CORE_PREFIXES, WEB_RESERVED_PATHS
from neops_compose.scenario import BASE_FILE, OVERLAYS, Scenario
from neops_compose.urls import BadUrl, PublicUrl

PLACEHOLDERS = {"changeme", "change_me", "unsafe", "password", "secret", "xxx"}
BASE_URLS = ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL")
BASE_SECRETS = ("NEOPS_CMS_DB_PASSWORD", "NEOPS_ENGINE_DB_PASSWORD", "DJANGO_SECRET_KEY", "NEOPS_ADMIN_PASSWORD")
OIDC_KEYS = ("NEOPS_OIDC_PROVIDER_ID", "NEOPS_OIDC_NAME", "NEOPS_OIDC_CLIENT_ID",
             "NEOPS_OIDC_CLIENT_SECRET", "NEOPS_OIDC_DISCOVERY_URL")
KEYCLOAK_SECRETS = ("NEOPS_KEYCLOAK_ADMIN_PASSWORD", "NEOPS_KEYCLOAK_DB_PASSWORD")
SECRET_HINT = "generate one with: openssl rand -hex 32"


def problems(env: Env, scenario: Scenario, repo: Path) -> list[str]:
    out: list[str] = []
    out += _overlay_problems(scenario, repo)
    out += _required(env, BASE_SECRETS, secret=True)
    urls, url_problems = _parse_urls(env, BASE_URLS)
    out += url_problems
    if scenario.tls == "files" and not env.flag("NEOPS_TLS_SELF_SIGNED"):
        out += _required(env, ("NEOPS_TLS_CERT_FILE", "NEOPS_TLS_KEY_FILE"))
    if scenario.tls == "acme":
        out += _required(env, ("NEOPS_ACME_EMAIL",))
        if env.get("NEOPS_HTTP_PORT", "80") != "80":
            out.append("NEOPS_HTTP_PORT must be 80 with compose.tls-acme.yaml: the HTTP-01 challenge has no other port")
    if scenario.oidc and not scenario.keycloak:
        out += _required(env, OIDC_KEYS, secret=False)
        out += _required(env, ("NEOPS_OIDC_CLIENT_SECRET",), secret=True)
    if scenario.keycloak:
        out += _required(env, KEYCLOAK_SECRETS, secret=True)
        kc, p = _parse_urls(env, ("NEOPS_KEYCLOAK_URL",))
        out += p
        urls.update(kc)
    if scenario.metrics:
        out += _required(env, ("NEOPS_GRAFANA_ADMIN_PASSWORD",), secret=True)
        if env.is_set("NEOPS_GRAFANA_URL"):
            gf, p = _parse_urls(env, ("NEOPS_GRAFANA_URL",))
            out += p
            urls.update(gf)
    if len(urls) >= len(BASE_URLS):
        out += _url_rules(urls, scenario)
    return out


def _overlay_problems(scenario: Scenario, repo: Path) -> list[str]:
    out = []
    if scenario.files[:1] != (BASE_FILE,):
        out.append(f"COMPOSE_FILE must start with {BASE_FILE}")
    for f in scenario.files:
        if not (repo / f).is_file():
            out.append(f"COMPOSE_FILE names {f}, which does not exist")
    for f in scenario.unknown_files:
        if (repo / f).is_file():
            out.append(f"COMPOSE_FILE names {f}, which is not a shipped overlay; local additions belong in compose.override.yaml")
    if scenario.proxy is None:
        out.append("COMPOSE_FILE must include compose.expose.yaml or compose.traefik.yaml")
    if scenario.has("expose") and scenario.has("traefik"):
        out.append("COMPOSE_FILE includes both compose.expose.yaml and compose.traefik.yaml: choose one, not both")
    if scenario.has("tls-files") and scenario.has("tls-acme"):
        out.append("COMPOSE_FILE must include at most one of compose.tls-files.yaml / compose.tls-acme.yaml")
    for name in ("tls-files", "tls-acme", "shared-host"):
        if scenario.has(name) and not scenario.has("traefik"):
            out.append(f"{OVERLAYS[name]} requires compose.traefik.yaml")
    if scenario.keycloak and not scenario.oidc:
        out.append("compose.keycloak.yaml requires compose.oidc.yaml")
    return out


def _required(env: Env, keys: tuple[str, ...], secret: bool = False) -> list[str]:
    out = []
    for key in keys:
        if not env.is_set(key):
            out.append(f"{key} is required" + (f" ({SECRET_HINT})" if secret else ""))
        elif secret and env.get(key).strip().lower() in PLACEHOLDERS:
            out.append(f"{key} is a placeholder value ({SECRET_HINT})")
    return out


def _parse_urls(env: Env, keys: tuple[str, ...]) -> tuple[dict[str, PublicUrl], list[str]]:
    urls: dict[str, PublicUrl] = {}
    out: list[str] = []
    for key in keys:
        if not env.is_set(key):
            out.append(f"{key} is required (the browser-facing URL)")
            continue
        try:
            urls[key] = PublicUrl.parse(env.get(key))
        except BadUrl as exc:
            out.append(f"{key}: {exc}")
    return urls, out


def _url_rules(urls: dict[str, PublicUrl], scenario: Scenario) -> list[str]:
    out = []
    web, cms = urls["NEOPS_WEB_URL"], urls["NEOPS_CMS_URL"]
    if cms.path:
        out.append("NEOPS_CMS_URL must not have a path: core cannot be served under a prefix (use the shared-host overlay with the web origin instead)")
    if cms.same_origin(web) and not scenario.shared_host:
        out.append("NEOPS_CMS_URL shares the web client's origin, which needs compose.traefik-shared-host.yaml in COMPOSE_FILE")
    if not cms.same_origin(web) and scenario.shared_host:
        out.append("compose.traefik-shared-host.yaml is in COMPOSE_FILE but NEOPS_CMS_URL is not the web client's origin")
    if urls["NEOPS_WORKFLOWS_URL"].same_origin(web):
        out.append("NEOPS_WORKFLOWS_URL must be a different origin (host or port) than NEOPS_WEB_URL: the web client disables the workflow manager on the same origin")
    reserved = CORE_PREFIXES + WEB_RESERVED_PATHS
    for key in ("NEOPS_ENGINE_URL", "NEOPS_KEYCLOAK_URL", "NEOPS_GRAFANA_URL"):
        u = urls.get(key)
        if u and u.path and any(u.path == r or u.path.startswith(r + "/") for r in reserved):
            out.append(f"{key} path {u.path} is reserved by core or the web client")
        if u and u.same_origin(web) and not u.path:
            out.append(f"{key} shares the web client's origin and needs a path prefix")
    if scenario.proxy == "traefik" and not scenario.shared_host:
        for key, u in urls.items():
            if u.path:
                out.append(f"{key} has a path but hostname-per-service routing is selected; paths need compose.traefik-shared-host.yaml")
    return out
```

- [ ] **Step 6: Run the tests** → `uv run pytest tests/unit/test_rules.py -q` → all pass (`14 passed`).

Note: `test_good_env_has_no_problems` needs the compose files to exist in `tmp_repo`; they are symlinked by the fixture from the real repo. Until Tasks 9–12 create them, create empty placeholders now so the suite runs, and let those tasks overwrite them:

```bash
for f in compose.yaml compose.expose.yaml compose.traefik.yaml compose.traefik-shared-host.yaml compose.tls-files.yaml compose.tls-acme.yaml compose.oidc.yaml compose.keycloak.yaml compose.metrics.yaml; do [ -f $f ] || printf 'services: {}\n' > $f; done
```

- [ ] **Step 7: Commit**

```bash
git add neops_compose/scenario.py neops_compose/routes.py neops_compose/rules.py tests/unit/test_rules.py compose*.yaml
git commit -qm "feat(cli): scenario model and the .env validation rules"
```

---
## Task 5: `traefik_model.py`

**Files:**
- Create: `neops_compose/traefik_model.py`
- Modify: `neops_compose/rules.py` (two more URL rules)
- Test: `tests/unit/test_traefik_model.py`, `tests/unit/test_rules.py`

- [ ] **Step 1: Add the scheme and monitor-port rules to `rules.py`**

Append these tests to `tests/unit/test_rules.py`:

```python
def test_traefik_with_tls_requires_https_urls(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace("https://engine.neops.example.com", "http://engine.neops.example.com"))
    assert any("NEOPS_ENGINE_URL" in p and "https" in p for p in problems(env, sc, tmp_repo))


def test_traefik_without_tls_requires_http_urls(tmp_repo):
    env, sc = make(tmp_repo, GOOD.replace(":compose.tls-files.yaml", "").replace("NEOPS_TLS_CERT_FILE=./certs/cert.pem\nNEOPS_TLS_KEY_FILE=./certs/key.pem\n", ""))
    assert any("NEOPS_WEB_URL" in p and "http://" in p for p in problems(env, sc, tmp_repo))


def test_shared_host_monitor_port_must_match_monitor_port_variable(tmp_repo):
    text = (GOOD.replace("compose.traefik.yaml:", "compose.traefik.yaml:compose.traefik-shared-host.yaml:")
            .replace("https://cms.neops.example.com", "https://neops.example.com")
            .replace("https://workflows.neops.example.com", "https://neops.example.com:9999"))
    env, sc = make(tmp_repo, text)
    assert any("NEOPS_MONITOR_PORT" in p for p in problems(env, sc, tmp_repo))
```

Add to the end of `_url_rules` in `neops_compose/rules.py` (before `return out`):

```python
    if scenario.proxy == "traefik":
        expected = "https" if scenario.tls else "http"
        for key, u in urls.items():
            if u.scheme != expected:
                out.append(f"{key} must use {expected}:// with this COMPOSE_FILE (TLS overlay {'present' if scenario.tls else 'absent'})")
        wf = urls["NEOPS_WORKFLOWS_URL"]
        if scenario.shared_host and wf.host == web.host and wf.port != monitor_port:
            out.append(f"NEOPS_WORKFLOWS_URL on the web hostname must use port NEOPS_MONITOR_PORT ({monitor_port})")
```

and give `_url_rules` the port: change its signature to `def _url_rules(urls, scenario, monitor_port: int)` and the call site to `_url_rules(urls, scenario, int(env.get("NEOPS_MONITOR_PORT", "8443")))`.

Run: `uv run pytest tests/unit/test_rules.py -q` → all pass.

- [ ] **Step 2: Write the failing model tests**

```python
# tests/unit/test_traefik_model.py
from neops_compose.env import Env
from neops_compose.scenario import Scenario
from neops_compose.traefik_model import build_traefik

HOSTS = """
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_KEYCLOAK_URL=https://auth.neops.example.com
"""
SHARED = """
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.traefik-shared-host.yaml:compose.tls-acme.yaml:compose.oidc.yaml:compose.keycloak.yaml
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://neops.example.com
NEOPS_ENGINE_URL=https://neops.example.com/engine
NEOPS_WORKFLOWS_URL=https://neops.example.com:8443
NEOPS_KEYCLOAK_URL=https://neops.example.com/sso
NEOPS_ACME_EMAIL=ops@example.com
"""


def cfg(tmp_path, text):
    (tmp_path / ".env").write_text(text)
    env = Env(tmp_path / ".env")
    return build_traefik(env, Scenario.from_env(env))


def by_name(cfg):
    return {r.name: r for r in cfg.routers}


def test_hosts_mode_routers_and_tls_files(tmp_path):
    c = cfg(tmp_path, HOSTS)
    r = by_name(c)
    assert r["web"].rule == "Host(`neops.example.com`)" and r["web"].entrypoint == "websecure" and r["web"].tls
    assert r["cms"].rule == "Host(`cms.neops.example.com`)" and r["cms"].service == "cms"
    assert r["engine"].middlewares == () and r["engine"].service == "engine"
    assert "keycloak" not in r  # no keycloak overlay in HOSTS
    assert r["engine-deny-worker-api"].priority > r["engine"].priority
    assert r["engine-deny-worker-api"].rule.startswith("Host(`engine.neops.example.com`) && Method(`POST`) && (")
    assert "Path(`/blackboard/job`)" in r["engine-deny-worker-api"].rule
    assert r["engine-deny-worker-api"].middlewares == ("deny-worker-api",)
    assert c.middlewares["deny-worker-api"] == {"ipAllowList": {"sourceRange": ["192.0.2.1/32"]}}
    assert [e.name for e in c.entrypoints] == ["web", "websecure"]
    assert c.entrypoints[0].redirect_to == "websecure"
    assert c.tls_mode == "files" and c.services["monitor"] == "http://monitor:80"


def test_shared_host_mode(tmp_path):
    c = cfg(tmp_path, SHARED)
    r = by_name(c)
    cms_routers = [x for x in c.routers if x.service == "cms"]
    assert len(cms_routers) == 10
    assert all(x.rule.startswith("Host(`neops.example.com`) && PathPrefix(`/") for x in cms_routers)
    assert all(x.priority > r["web"].priority for x in cms_routers)
    assert r["engine"].rule == "Host(`neops.example.com`) && PathPrefix(`/engine`)"
    assert r["engine"].middlewares == ("engine-strip",)
    assert c.middlewares["engine-strip"] == {"stripPrefix": {"prefixes": ["/engine"]}}
    assert "PathRegexp(`^/engine/workers/[^/]+/(ping|unregister)$`)" in r["engine-deny-worker-api"].rule
    assert r["monitor"].entrypoint == "monitor" and r["monitor"].rule == "Host(`neops.example.com`)"
    assert r["keycloak"].middlewares == () and r["keycloak"].rule.endswith("PathPrefix(`/sso`)")
    assert [e.name for e in c.entrypoints] == ["web", "websecure", "monitor"]
    assert c.tls_mode == "acme" and c.acme_email == "ops@example.com"


def test_http_only_uses_web_entrypoint_without_tls(tmp_path):
    text = HOSTS.replace(":compose.tls-files.yaml", "").replace("https://", "http://")
    c = cfg(tmp_path, text)
    assert all(x.entrypoint == "web" and not x.tls for x in c.routers)
    assert [e.name for e in c.entrypoints] == ["web"] and c.entrypoints[0].redirect_to is None


def test_model_is_deterministic(tmp_path):
    assert cfg(tmp_path, SHARED) == cfg(tmp_path, SHARED)
```

- [ ] **Step 3: Run to verify failure** → `uv run pytest tests/unit/test_traefik_model.py -q` → collection error.

- [ ] **Step 4: Write `neops_compose/traefik_model.py`**

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field

from neops_compose.env import Env
from neops_compose.routes import CORE_PREFIXES, ENGINE_PUBLIC_WORKER_ROUTES
from neops_compose.scenario import Scenario
from neops_compose.urls import PublicUrl

SERVICE_URLS = {
    "web": "http://web:8080",
    "cms": "http://cms:8000",
    "engine": "http://engine:3030",
    "monitor": "http://monitor:80",
    "keycloak": "http://keycloak:8080",
    "grafana": "http://grafana:3000",
}
DENY_MIDDLEWARE = "deny-worker-api"
# TEST-NET-1: never routable, so the allow-list matches nobody and Traefik answers 403.
DENY_RANGE = "192.0.2.1/32"


@dataclass(frozen=True)
class EntryPoint:
    name: str
    address: str
    redirect_to: str | None = None


@dataclass(frozen=True)
class Router:
    name: str
    rule: str
    entrypoint: str
    service: str
    priority: int
    middlewares: tuple[str, ...] = ()
    tls: bool = False


@dataclass(frozen=True)
class TraefikConfig:
    entrypoints: tuple[EntryPoint, ...]
    routers: tuple[Router, ...]
    services: dict[str, str]
    middlewares: dict[str, dict]
    tls_mode: str | None
    acme_email: str = ""


def _host_rule(url: PublicUrl) -> str:
    rule = f"Host(`{url.host}`)"
    if url.path:
        rule += f" && PathPrefix(`{url.path}`)"
    return rule


def worker_deny_rule(host: str, prefix: str) -> str:
    parts = []
    for kind, value in ENGINE_PUBLIC_WORKER_ROUTES:
        if kind == "path":
            parts.append(f"Path(`{prefix}{value}`)")
        elif kind == "prefix":
            parts.append(f"PathPrefix(`{prefix}{value}`)")
        else:
            parts.append(f"PathRegexp(`^{re.escape(prefix)}{value}$`)")
    return f"Host(`{host}`) && Method(`POST`) && (" + " || ".join(parts) + ")"


def build_traefik(env: Env, scenario: Scenario) -> TraefikConfig:
    tls = scenario.tls
    secure = tls is not None
    monitor_port = int(env.get("NEOPS_MONITOR_PORT", "8443"))
    urls = {k: PublicUrl.parse(env.require(k)) for k in
            ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL")}
    if scenario.keycloak:
        urls["NEOPS_KEYCLOAK_URL"] = PublicUrl.parse(env.require("NEOPS_KEYCLOAK_URL"))
    if scenario.metrics and env.is_set("NEOPS_GRAFANA_URL"):
        urls["NEOPS_GRAFANA_URL"] = PublicUrl.parse(env.get("NEOPS_GRAFANA_URL"))

    entrypoints = [EntryPoint("web", ":80", "websecure" if secure else None)]
    if secure:
        entrypoints.append(EntryPoint("websecure", ":443"))
    if scenario.shared_host:
        entrypoints.append(EntryPoint("monitor", ":8443"))
    default_ep = "websecure" if secure else "web"

    def entrypoint_for(url: PublicUrl) -> str:
        if scenario.shared_host and url.port == monitor_port and url.host == urls["NEOPS_WEB_URL"].host:
            return "monitor"
        return default_ep

    routers: list[Router] = []
    middlewares: dict[str, dict] = {}
    services: dict[str, str] = {}

    def add(name: str, rule: str, service: str, priority: int, ep: str, mws: tuple[str, ...] = ()) -> None:
        routers.append(Router(name, rule, ep, service, priority, mws, tls=ep != "web"))
        services[service] = SERVICE_URLS[service]

    web = urls["NEOPS_WEB_URL"]
    add("web", _host_rule(web), "web", 1, entrypoint_for(web))

    cms = urls["NEOPS_CMS_URL"]
    if scenario.shared_host:
        for prefix in CORE_PREFIXES:
            slug = prefix.strip("/").replace("/", "-").replace(".", "")
            add(f"cms-{slug}", f"Host(`{web.host}`) && PathPrefix(`{prefix}`)", "cms", 100, entrypoint_for(cms))
    else:
        add("cms", _host_rule(cms), "cms", 10, entrypoint_for(cms))

    engine = urls["NEOPS_ENGINE_URL"]
    engine_mws: tuple[str, ...] = ()
    if engine.path:
        middlewares["engine-strip"] = {"stripPrefix": {"prefixes": [engine.path]}}
        engine_mws = ("engine-strip",)
    add("engine", _host_rule(engine), "engine", 10, entrypoint_for(engine), engine_mws)
    middlewares[DENY_MIDDLEWARE] = {"ipAllowList": {"sourceRange": [DENY_RANGE]}}
    add("engine-deny-worker-api", worker_deny_rule(engine.host, engine.path), "engine", 1000,
        entrypoint_for(engine), (DENY_MIDDLEWARE,))

    monitor = urls["NEOPS_WORKFLOWS_URL"]
    add("monitor", _host_rule(monitor), "monitor", 10, entrypoint_for(monitor))

    if "NEOPS_KEYCLOAK_URL" in urls:
        kc = urls["NEOPS_KEYCLOAK_URL"]
        add("keycloak", _host_rule(kc), "keycloak", 10, entrypoint_for(kc))
    if "NEOPS_GRAFANA_URL" in urls:
        gf = urls["NEOPS_GRAFANA_URL"]
        add("grafana", _host_rule(gf), "grafana", 10, entrypoint_for(gf))

    return TraefikConfig(
        entrypoints=tuple(entrypoints),
        routers=tuple(routers),
        services=dict(sorted(services.items())),
        middlewares=dict(sorted(middlewares.items())),
        tls_mode=tls,
        acme_email=env.get("NEOPS_ACME_EMAIL") if tls == "acme" else "",
    )


def static_config(cfg: TraefikConfig) -> dict:
    eps: dict[str, dict] = {}
    for ep in cfg.entrypoints:
        entry: dict = {"address": ep.address}
        if ep.redirect_to:
            entry["http"] = {"redirections": {"entryPoint": {"to": ep.redirect_to, "scheme": "https"}}}
        eps[ep.name] = entry
    out: dict = {
        "entryPoints": eps,
        "providers": {"file": {"filename": "/etc/traefik/dynamic.yml", "watch": True}},
        "api": {"dashboard": False},
        "accessLog": {},
        "log": {"level": "INFO"},
    }
    if cfg.tls_mode == "acme":
        out["certificatesResolvers"] = {"letsencrypt": {"acme": {
            "email": cfg.acme_email, "storage": "/acme/acme.json", "httpChallenge": {"entryPoint": "web"}}}}
    return out


def dynamic_config(cfg: TraefikConfig) -> dict:
    routers: dict[str, dict] = {}
    for r in cfg.routers:
        entry: dict = {"rule": r.rule, "entryPoints": [r.entrypoint], "service": r.service, "priority": r.priority}
        if r.middlewares:
            entry["middlewares"] = list(r.middlewares)
        if r.tls:
            entry["tls"] = {"certResolver": "letsencrypt"} if cfg.tls_mode == "acme" else {}
        routers[r.name] = entry
    out: dict = {"http": {
        "routers": routers,
        "services": {name: {"loadBalancer": {"servers": [{"url": url}]}} for name, url in cfg.services.items()},
        "middlewares": cfg.middlewares,
    }}
    if cfg.tls_mode == "files":
        cert = {"certFile": "/etc/traefik/certs/cert.pem", "keyFile": "/etc/traefik/certs/key.pem"}
        out["tls"] = {"certificates": [cert], "stores": {"default": {"defaultCertificate": cert}}}
    return out
```

- [ ] **Step 5: Run the tests** → `uv run pytest tests/unit/test_traefik_model.py tests/unit/test_rules.py -q` → all pass.

- [ ] **Step 6: Commit**

```bash
git add neops_compose/traefik_model.py neops_compose/rules.py tests/unit/test_traefik_model.py tests/unit/test_rules.py
git commit -qm "feat(cli): Traefik routing model for hostname and shared-host modes with the worker-API deny rule"
```

---

## Task 6: `secrets.py`

**Files:**
- Create: `neops_compose/secrets.py`
- Test: `tests/unit/test_secrets.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_secrets.py
import stat

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from neops_compose import secrets


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_ensure_jwt_creates_pkcs8_and_spki_once(tmp_path):
    d = tmp_path / "jwt"
    assert secrets.ensure_jwt(d) is True
    priv, pub = d / "private.pem", d / "public.pem"
    assert priv.read_text().startswith("-----BEGIN PRIVATE KEY-----")
    assert pub.read_text().startswith("-----BEGIN PUBLIC KEY-----")
    assert mode(priv) == 0o600 and mode(d) == 0o700
    before = priv.read_bytes()
    assert secrets.ensure_jwt(d) is False and priv.read_bytes() == before
    key = serialization.load_pem_private_key(before, password=None)
    assert key.key_size == 2048


def test_keycloak_client_secret_is_stable(tmp_path):
    p = tmp_path / "keycloak-client.env"
    first = secrets.ensure_keycloak_client_secret(p)
    assert len(first) == 64 and p.read_text() == f"NEOPS_KEYCLOAK_CLIENT_SECRET={first}\n"
    assert secrets.ensure_keycloak_client_secret(p) == first
    assert secrets.read_keycloak_client_secret(p) == first and mode(p) == 0o600


def test_selfsigned_cert_covers_hosts_and_detects_staleness(tmp_path):
    d = tmp_path / "tls"
    assert secrets.ensure_selfsigned(d, ["neops.example.com", "cms.neops.example.com"]) is True
    cert = x509.load_pem_x509_certificate((d / "cert.pem").read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert set(sans) == {"neops.example.com", "cms.neops.example.com"}
    assert (d / "ca.pem").exists() and (d / "key.pem").exists() and mode(d / "key.pem") == 0o600
    assert secrets.selfsigned_sans(d / "cert.pem") == {"neops.example.com", "cms.neops.example.com"}
    assert secrets.stale_sans(d / "cert.pem", ["neops.example.com", "new.example.com"]) == {"new.example.com"}
    assert secrets.ensure_selfsigned(d, ["neops.example.com"]) is False  # never overwrites
    assert secrets.ensure_selfsigned(d, ["neops.example.com", "new.example.com"], rotate=True) is True
    assert secrets.stale_sans(d / "cert.pem", ["new.example.com"]) == set()
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/unit/test_secrets.py -q` → collection error.

- [ ] **Step 3: Write `neops_compose/secrets.py`**

```python
from __future__ import annotations

import datetime as dt
import os
import secrets as pysecrets
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CLIENT_SECRET_KEY = "NEOPS_KEYCLOAK_CLIENT_SECRET"


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_secret(path: Path, data: bytes) -> None:
    private_dir(path.parent)
    path.write_bytes(data)
    os.chmod(path, 0o600)


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_private(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def ensure_jwt(jwt_dir: Path) -> bool:
    """RSA-2048 keypair the CMS signs tokens with and the engine verifies. Never overwrites."""
    priv, pub = jwt_dir / "private.pem", jwt_dir / "public.pem"
    if priv.exists() and pub.exists():
        return False
    key = _rsa_key()
    write_secret(priv, _pem_private(key))
    write_secret(pub, key.public_key().public_bytes(serialization.Encoding.PEM,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo))
    return True


def read_keycloak_client_secret(path: Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if line.startswith(CLIENT_SECRET_KEY + "="):
            return line.split("=", 1)[1].strip()
    return None


def ensure_keycloak_client_secret(path: Path, rotate: bool = False) -> str:
    existing = read_keycloak_client_secret(path)
    if existing and not rotate:
        return existing
    value = pysecrets.token_hex(32)
    write_secret(path, f"{CLIENT_SECRET_KEY}={value}\n".encode())
    return value


def selfsigned_sans(cert_path: Path) -> set[str]:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    return set(ext.value.get_values_for_type(x509.DNSName))


def stale_sans(cert_path: Path, hosts: list[str]) -> set[str]:
    return set(hosts) - selfsigned_sans(cert_path)


def ensure_selfsigned(tls_dir: Path, hosts: list[str], rotate: bool = False, days: int = 1095) -> bool:
    """A private CA plus one server certificate carrying every public hostname as a SAN."""
    cert_path = tls_dir / "cert.pem"
    if cert_path.exists() and not rotate:
        return False
    now = dt.datetime.now(dt.UTC)
    ca_key = _rsa_key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Neops deployment CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days * 2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = _rsa_key()
    hosts = sorted(set(hosts))
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in hosts]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    write_secret(tls_dir / "ca.pem", ca.public_bytes(pem))
    write_secret(tls_dir / "key.pem", _pem_private(key))
    write_secret(cert_path, cert.public_bytes(pem) + ca.public_bytes(pem))
    return True
```

- [ ] **Step 4: Run the tests** → `uv run pytest tests/unit/test_secrets.py -q` → `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add neops_compose/secrets.py tests/unit/test_secrets.py
git commit -qm "feat(cli): JWT keypair, self-signed TLS and Keycloak client secret generation"
```

---

## Task 7: `render.py`

**Files:**
- Create: `neops_compose/render.py`
- Test: `tests/unit/test_render.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_render.py
import json
import stat

import yaml

from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.render import render
from neops_compose.scenario import Scenario

HOSTS = """
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
"""
KEYCLOAK = HOSTS.replace("compose.tls-files.yaml", "compose.tls-files.yaml:compose.oidc.yaml:compose.keycloak.yaml") + \
    "NEOPS_KEYCLOAK_URL=https://auth.neops.example.com/sso\n"
EXTERNAL = HOSTS.replace("compose.tls-files.yaml", "compose.tls-files.yaml:compose.oidc.yaml") + """
NEOPS_OIDC_PROVIDER_ID=entra
NEOPS_OIDC_NAME=Company SSO
NEOPS_OIDC_CLIENT_ID=abc
NEOPS_OIDC_CLIENT_SECRET=s3cr3t
NEOPS_OIDC_DISCOVERY_URL=https://login.example.com/.well-known/openid-configuration
"""
EXPOSE_SHARED = """
COMPOSE_FILE=compose.yaml:compose.expose.yaml
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://neops.example.com
NEOPS_ENGINE_URL=https://neops.example.com/engine
NEOPS_WORKFLOWS_URL=https://neops.example.com:8443
"""


def run(tmp_repo, text):
    (tmp_repo / ".env").write_text(text)
    env = Env(tmp_repo / ".env")
    paths = Paths.for_repo(tmp_repo, env)
    paths.secrets.mkdir(parents=True, exist_ok=True)
    (paths.keycloak_client_env).write_text("NEOPS_KEYCLOAK_CLIENT_SECRET=kcsecret\n")
    return render(env, Scenario.from_env(env), paths), paths


def envfile(p):
    return dict(line.split("=", 1) for line in p.read_text().splitlines() if line and not line.startswith("#"))


def test_cms_env_hosts_mode(tmp_repo):
    written, paths = run(tmp_repo, HOSTS)
    cms = envfile(paths.generated / "cms.env")
    assert cms["DJANGO_ALLOWED_HOSTS"] == "cms.neops.example.com,neops.example.com,cms,localhost,127.0.0.1"
    assert cms["CORS_ORIGIN_ALLOW_ALL"] == "True"
    assert cms["ACCOUNT_DEFAULT_HTTP_PROTOCOL"] == "https"
    assert stat.S_IMODE(paths.generated.stat().st_mode) == 0o700
    assert stat.S_IMODE((paths.generated / "cms.env").stat().st_mode) == 0o600
    assert (paths.generated / "traefik" / "traefik.yml").exists()
    dyn = yaml.safe_load((paths.generated / "traefik" / "dynamic.yml").read_text())
    assert dyn["http"]["routers"]["cms"]["rule"] == "Host(`cms.neops.example.com`)"
    assert dyn["tls"]["stores"]["default"]["defaultCertificate"]["certFile"] == "/etc/traefik/certs/cert.pem"
    assert not (paths.generated / "providers.json").exists()


def test_cms_env_shared_origin_has_no_cors_and_no_traefik(tmp_repo):
    written, paths = run(tmp_repo, EXPOSE_SHARED)
    cms = envfile(paths.generated / "cms.env")
    assert cms["DJANGO_ALLOWED_HOSTS"] == "neops.example.com,cms,localhost,127.0.0.1"
    assert "CORS_ORIGIN_ALLOW_ALL" not in cms
    assert not (paths.generated / "traefik").exists()


def test_external_oidc_providers(tmp_repo):
    _, paths = run(tmp_repo, EXTERNAL)
    doc = json.loads((paths.generated / "providers.json").read_text())
    assert doc == {"providers": [{"provider_id": "entra", "name": "Company SSO", "client_id": "abc", "secret": "s3cr3t",
                                  "settings": {"server_url": "https://login.example.com/.well-known/openid-configuration"}}]}


def test_keycloak_realm_and_providers(tmp_repo):
    _, paths = run(tmp_repo, KEYCLOAK)
    assert envfile(paths.generated / "keycloak.env") == {"KC_HTTP_RELATIVE_PATH": "/sso"}
    doc = json.loads((paths.generated / "providers.json").read_text())
    p = doc["providers"][0]
    assert p["provider_id"] == "keycloak" and p["client_id"] == "neops-auth" and p["secret"] == "kcsecret"
    assert p["settings"]["server_url"] == "http://keycloak:8080/sso/realms/neops/.well-known/openid-configuration"
    realm = json.loads((paths.generated / "keycloak" / "realm.json").read_text())
    client = realm["clients"][0]
    assert realm["realm"] == "neops" and client["clientId"] == "neops-auth" and client["secret"] == "kcsecret"
    assert client["redirectUris"] == ["https://cms.neops.example.com/accounts/oidc/keycloak/login/callback/"]
    assert client["attributes"]["post.logout.redirect.uris"] == "https://neops.example.com/*"
    mapper = client["protocolMappers"][0]
    assert mapper["protocolMapper"] == "oidc-usermodel-client-role-mapper"
    assert mapper["config"]["id.token.claim"] == "true" and mapper["config"]["userinfo.token.claim"] == "true"


def test_render_is_deterministic_and_cleans_stale_files(tmp_repo):
    _, paths = run(tmp_repo, KEYCLOAK)
    first = {p: p.read_bytes() for p in paths.generated.rglob("*") if p.is_file()}
    run(tmp_repo, HOSTS)
    assert not (paths.generated / "keycloak").exists() and not (paths.generated / "providers.json").exists()
    run(tmp_repo, KEYCLOAK)
    second = {p: p.read_bytes() for p in paths.generated.rglob("*") if p.is_file()}
    assert first == second
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/unit/test_render.py -q` → collection error.

- [ ] **Step 3: Write `neops_compose/render.py`**

```python
from __future__ import annotations

import json
import shutil
from pathlib import Path

from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.secrets import private_dir, read_keycloak_client_secret, write_secret
from neops_compose.traefik_model import build_traefik, dynamic_config, static_config
from neops_compose.urls import PublicUrl

KEYCLOAK_REALM = "neops"
KEYCLOAK_CLIENT_ID = "neops-auth"
KEYCLOAK_PROVIDER_ID = "keycloak"


def render(env: Env, scenario: Scenario, paths: Paths) -> list[Path]:
    """Rebuild generated/ from .env. Deterministic; removes what the scenario no longer needs."""
    out = paths.generated
    if out.exists():
        shutil.rmtree(out)
    private_dir(out)
    written: list[Path] = []

    def emit(rel: str, text: str) -> None:
        p = out / rel
        write_secret(p, text.encode())
        written.append(p)

    emit("cms.env", cms_env(env, scenario))
    if scenario.proxy == "traefik":
        cfg = build_traefik(env, scenario)
        emit("traefik/traefik.yml", _yaml_json(static_config(cfg)))
        emit("traefik/dynamic.yml", _yaml_json(dynamic_config(cfg)))
    if scenario.keycloak:
        emit("keycloak.env", f"KC_HTTP_RELATIVE_PATH={keycloak_relative_path(env)}\n")
        emit("keycloak/realm.json", json.dumps(keycloak_realm(env, paths), indent=2) + "\n")
    if scenario.oidc:
        emit("providers.json", json.dumps(providers(env, scenario, paths), indent=2) + "\n")
    return written


def _yaml_json(doc: dict) -> str:
    # JSON is valid YAML; Traefik parses the .yml extension with a YAML reader.
    return json.dumps(doc, indent=2) + "\n"


def cms_env(env: Env, scenario: Scenario) -> str:
    web = PublicUrl.parse(env.require("NEOPS_WEB_URL"))
    cms = PublicUrl.parse(env.require("NEOPS_CMS_URL"))
    hosts = [cms.host]
    if web.host != cms.host:
        hosts.append(web.host)
    hosts += ["cms", "localhost", "127.0.0.1"]
    lines = [
        "# Generated by ./neops render from .env. Do not edit.",
        f"DJANGO_ALLOWED_HOSTS={','.join(hosts)}",
        f"ACCOUNT_DEFAULT_HTTP_PROTOCOL={web.scheme}",
    ]
    if not web.same_origin(cms):
        lines.append("CORS_ORIGIN_ALLOW_ALL=True")
    return "\n".join(lines) + "\n"


def keycloak_relative_path(env: Env) -> str:
    return PublicUrl.parse(env.require("NEOPS_KEYCLOAK_URL")).path or "/"


def providers(env: Env, scenario: Scenario, paths: Paths) -> dict:
    if scenario.keycloak:
        rel = keycloak_relative_path(env).rstrip("/")
        entry = {
            "provider_id": KEYCLOAK_PROVIDER_ID,
            "name": "Keycloak",
            "client_id": KEYCLOAK_CLIENT_ID,
            "secret": read_keycloak_client_secret(paths.keycloak_client_env) or "",
            "settings": {"server_url": f"http://keycloak:8080{rel}/realms/{KEYCLOAK_REALM}/.well-known/openid-configuration"},
        }
    else:
        entry = {
            "provider_id": env.require("NEOPS_OIDC_PROVIDER_ID"),
            "name": env.require("NEOPS_OIDC_NAME"),
            "client_id": env.require("NEOPS_OIDC_CLIENT_ID"),
            "secret": env.require("NEOPS_OIDC_CLIENT_SECRET"),
            "settings": {"server_url": env.require("NEOPS_OIDC_DISCOVERY_URL")},
        }
    return {"providers": [entry]}


def keycloak_realm(env: Env, paths: Paths) -> dict:
    web = PublicUrl.parse(env.require("NEOPS_WEB_URL"))
    cms = PublicUrl.parse(env.require("NEOPS_CMS_URL"))
    secret = read_keycloak_client_secret(paths.keycloak_client_env) or ""
    return {
        "realm": KEYCLOAK_REALM,
        "enabled": True,
        "sslRequired": "external",
        "registrationAllowed": False,
        "clients": [{
            "clientId": KEYCLOAK_CLIENT_ID,
            "name": "Neops",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "clientAuthenticatorType": "client-secret",
            "secret": secret,
            "standardFlowEnabled": True,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "redirectUris": [f"{cms}/accounts/oidc/{KEYCLOAK_PROVIDER_ID}/login/callback/"],
            "webOrigins": [],
            "attributes": {"post.logout.redirect.uris": f"{web}/*"},
            "defaultClientScopes": ["profile", "email", "roles", "web-origins"],
            "protocolMappers": [{
                "name": "neops client roles (id+userinfo)",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-usermodel-client-role-mapper",
                "consentRequired": False,
                "config": {
                    "claim.name": "resource_access.${client_id}.roles",
                    "jsonType.label": "String",
                    "multivalued": "true",
                    "id.token.claim": "true",
                    "access.token.claim": "true",
                    "userinfo.token.claim": "true",
                    "usermodel.clientRoleMapping.clientId": KEYCLOAK_CLIENT_ID,
                },
            }],
        }],
    }
```

- [ ] **Step 4: Run the tests** → `uv run pytest tests/unit/test_render.py -q` → `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add neops_compose/render.py tests/unit/test_render.py
git commit -qm "feat(cli): render generated/ (cms env, Traefik config, OIDC seed, Keycloak realm)"
```

---
## Task 8: `compose.yaml` (base stack)

**Files:**
- Create: `compose.yaml` (overwrite the placeholder from Task 4)
- Test: `tests/unit/test_compose_files.py` (Task 11 completes it; write the first assertion now)

- [ ] **Step 1: Write the failing invariant test**

```python
# tests/unit/test_compose_files.py
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILES = sorted(REPO.glob("compose*.yaml"))
ALLOWED_BIND_ROOTS = ("${NEOPS_DATA_DIR:-./data}/", "./generated/", "./certs/", "./cust-cert/", "./metrics/",
                      "${NEOPS_TLS_CERT_FILE:-./certs/cert.pem}", "${NEOPS_TLS_KEY_FILE:-./certs/key.pem}")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def test_no_named_volumes_anywhere():
    for f in COMPOSE_FILES:
        doc = load(f)
        assert "volumes" not in doc, f"{f.name} declares top-level volumes"
        for name, svc in (doc.get("services") or {}).items():
            for v in svc.get("volumes") or []:
                src = v.split(":")[0] if isinstance(v, str) else v.get("source", "")
                assert src.startswith(ALLOWED_BIND_ROOTS), f"{f.name}: service {name} mounts {src!r} outside the bind roots"


def test_base_stack_has_the_expected_services():
    doc = load(REPO / "compose.yaml")
    assert set(doc["services"]) == {"postgres-cms", "postgres-engine", "redis", "elasticsearch", "cms-init", "cms",
                                    "cms-worker", "cms-beat", "engine", "monitor", "worker", "web"}
    for name, svc in doc["services"].items():
        assert "ports" not in svc, f"base file must publish nothing, {name} does"
        assert svc.get("logging", {}).get("driver") == "json-file", f"{name} lacks the logging block"


def test_no_interpolation_of_generated_only_keys():
    generated_only = {"DJANGO_ALLOWED_HOSTS", "CORS_ORIGIN_ALLOW_ALL", "ACCOUNT_DEFAULT_HTTP_PROTOCOL", "KC_HTTP_RELATIVE_PATH"}
    for f in COMPOSE_FILES:
        for key in generated_only:
            assert not re.search(r"\$\{?" + key, f.read_text()), f"{f.name} interpolates {key}, which only exists in generated/"
```

Run: `uv run pytest tests/unit/test_compose_files.py -q` → `test_base_stack_has_the_expected_services` FAILS (placeholder file).

- [ ] **Step 2: Write `compose.yaml`**

```yaml
# Neops 2.0 production stack. Scenario overlays are selected with COMPOSE_FILE in .env;
# see .env.example and examples/. Operate it with ./neops (see README.md).
#
# Image pins: the tested set for this release. Override per image from .env only in
# an emergency (NEOPS_CORE_TAG, ...). NEOPS_*_IMAGE replaces the whole reference.
x-versions:
  core: &core-image ${NEOPS_CORE_IMAGE:-quay.io/zebbra/neops-core:${NEOPS_CORE_TAG:-2.1.0-beta.5}}
  engine: &engine-image ${NEOPS_ENGINE_IMAGE:-quay.io/zebbra/neops-workflow-engine:${NEOPS_ENGINE_TAG:-0.43.0-beta.2}}
  monitor: &monitor-image ${NEOPS_MONITOR_IMAGE:-quay.io/zebbra/neops-monitor-app:${NEOPS_MONITOR_TAG:-0.43.0-beta.2}}
  worker: &worker-image ${NEOPS_WORKER_IMAGE:-quay.io/zebbra/neops-worker-sdk:${NEOPS_WORKER_TAG:-0.2.0-beta.5}}
  web: &web-image ${NEOPS_WEB_IMAGE:-quay.io/zebbra/neops-web-client:${NEOPS_WEB_TAG:-5.0.0}}

x-logging: &logging
  driver: json-file
  options:
    max-size: "20m"
    max-file: "5"

x-core-env: &core-env
  DEBUG: "False"
  DATABASE_URL: postgres://neops:${NEOPS_CMS_DB_PASSWORD}@postgres-cms:5432/neops
  REDIS_URL: redis://redis:6379/0
  ELASTICSEARCH_HOSTS: http://elasticsearch:9200
  ELASTICSEARCH_DSL_INDEX_SHARDS: "1"
  ELASTICSEARCH_DSL_INDEX_REPLICAS: "0"
  ELASTICSEARCH_DSL_INDEX_FIELDS_LIMIT: "10000"
  DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY}
  DJANGO_ALLOW_ASYNC_UNSAFE: "True"
  NEOPS_PLUGINS: neops_cron neops_auth_static_api_key neops_auth_django neops_permissions_simple neops_reports
  NEOPS_JWT_PRIVATE_KEY_PATH: /etc/neops/jwt/private.pem
  NEOPS_JWT_PUBLIC_KEY_PATH: /etc/neops/jwt/public.pem
  NEOPS_PUBLIC_URL: ${NEOPS_CMS_URL}
  NEOPS_ADMIN_USER: ${NEOPS_ADMIN_USER:-neops}
  NEOPS_ADMIN_EMAIL: ${NEOPS_ADMIN_EMAIL:-neops@example.com}
  NEOPS_ADMIN_PASSWORD: ${NEOPS_ADMIN_PASSWORD}
  SENTRY_DSN: ${SENTRY_DSN:-}
  SENTRY_ENVIRONMENT: ${SENTRY_ENVIRONMENT:-}
  EMAIL_URL: ${EMAIL_URL:-}
  NEOPS_LOGGER_CLASS: ${NEOPS_LOGGER_CLASS:-neops.core.log.loggers.db.DbLogger}
  EXECUTION_LOG_RETENTION_ENABLED: ${EXECUTION_LOG_RETENTION_ENABLED:-False}
  EXECUTION_LOG_RETENTION_DAYS: ${EXECUTION_LOG_RETENTION_DAYS:-90}
  FACT_CLEANUP_ENABLED: ${FACT_CLEANUP_ENABLED:-False}
  NEOPS_EXECUTION_CLEANUP_ENABLED: ${NEOPS_EXECUTION_CLEANUP_ENABLED:-False}
  NORNIR_THREADS: ${NORNIR_THREADS:-50}
  NORNIR_CLOSE_CONNECTION: ${NORNIR_CLOSE_CONNECTION:-True}
  NORNIR_CONNECTION_DELAY_FACTOR: ${NORNIR_CONNECTION_DELAY_FACTOR:-2}

x-core-service: &core-service
  image: *core-image
  env_file:
    - ./generated/cms.env
  volumes:
    - ${NEOPS_DATA_DIR:-./data}/secrets/jwt:/etc/neops/jwt:ro
    - ./cust-cert:/cust-cert:ro
  logging: *logging
  restart: unless-stopped

services:
  postgres-cms:
    image: postgres:16-alpine
    command: postgres -c max_connections=300
    environment:
      POSTGRES_USER: neops
      POSTGRES_DB: neops
      POSTGRES_PASSWORD: ${NEOPS_CMS_DB_PASSWORD}
    volumes:
      - ${NEOPS_DATA_DIR:-./data}/cms/postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U neops -d neops"]
      interval: 10s
      timeout: 5s
      retries: 6
    logging: *logging
    restart: unless-stopped

  postgres-engine:
    # The engine connects as `postgres` to the database `neops-workflow`; both are hardcoded in the engine.
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: postgres
      POSTGRES_DB: neops-workflow
      POSTGRES_PASSWORD: ${NEOPS_ENGINE_DB_PASSWORD}
    volumes:
      - ${NEOPS_DATA_DIR:-./data}/engine/postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d neops-workflow"]
      interval: 10s
      timeout: 5s
      retries: 6
    logging: *logging
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 6
    logging: *logging
    restart: unless-stopped

  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.9.2
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - bootstrap.memory_lock=true
      - ES_JAVA_OPTS=-Xms${NEOPS_ES_HEAP:-1g} -Xmx${NEOPS_ES_HEAP:-1g}
      - cluster.routing.allocation.disk.watermark.low=85%
      - cluster.routing.allocation.disk.watermark.high=90%
      - cluster.routing.allocation.disk.watermark.flood_stage=95%
    ulimits:
      memlock:
        soft: -1
        hard: -1
      nofile:
        soft: 65536
        hard: 65536
    volumes:
      # Must be owned by uid 1000; ./neops migrate (0001_initial_layout) takes care of it.
      - ${NEOPS_DATA_DIR:-./data}/elasticsearch:/usr/share/elasticsearch/data
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:9200/_cluster/health >/dev/null || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 6
      start_period: 90s
    logging: *logging
    restart: unless-stopped

  cms-init:
    # One-shot: Django migrations, Elasticsearch indices, the superuser. Re-runs on every `up` (idempotent).
    # Does not use the image's init.sh, which exits 1 on a fresh database (neops-core #2269).
    <<: *core-service
    environment: *core-env
    restart: "no"
    depends_on:
      postgres-cms:
        condition: service_healthy
      elasticsearch:
        condition: service_healthy
      redis:
        condition: service_healthy
    command:
      - /bin/sh
      - -c
      - |
        set -e
        python manage.py migrate --noinput
        if ! out=$$(python manage.py elastic_index --create 2>&1); then
          echo "$$out" | grep -q resource_already_exists_exception || { echo "$$out"; exit 1; }
        fi
        python manage.py shell -c "import os; from django.contrib.auth import get_user_model; U = get_user_model(); u, created = U.objects.get_or_create(username=os.environ['NEOPS_ADMIN_USER'], defaults={'email': os.environ['NEOPS_ADMIN_EMAIL'], 'is_staff': True, 'is_superuser': True}); created and (u.set_password(os.environ['NEOPS_ADMIN_PASSWORD']), u.save()); print('superuser', 'created' if created else 'present')"

  cms:
    <<: *core-service
    environment: *core-env
    volumes:
      - ${NEOPS_DATA_DIR:-./data}/secrets/jwt:/etc/neops/jwt:ro
      - ./cust-cert:/cust-cert:ro
      - ${NEOPS_DATA_DIR:-./data}/cms/media:/app/backend/media
      - ${NEOPS_DATA_DIR:-./data}/cms/tmp:/tmp
    depends_on:
      cms-init:
        condition: service_completed_successfully
      redis:
        condition: service_healthy
    healthcheck:
      # `localhost` is in the generated DJANGO_ALLOWED_HOSTS for exactly this check.
      test: ["CMD-SHELL", "curl -fsS http://localhost:8000/admin/login/ >/dev/null || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 6
      start_period: 150s

  cms-worker:
    <<: *core-service
    environment:
      <<: *core-env
      ELASTICSEARCH_DSL_SIGNAL_PROCESSOR: neops.core.documents.neops_signal_processor.NeopsCacheSignalProcessor
    command: ["/usr/local/bin/celery", "-A", "neopsapp", "worker", "--concurrency", "${NEOPS_CMS_WORKER_CONCURRENCY:-2}",
              "-l", "INFO", "--prefetch-multiplier", "1", "-O", "fair", "--without-heartbeat"]
    depends_on:
      cms:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "celery -A neopsapp inspect ping -d celery@$$HOSTNAME >/dev/null || exit 1"]
      interval: 60s
      timeout: 15s
      retries: 3
      start_period: 90s

  cms-beat:
    <<: *core-service
    environment: *core-env
    command: ["/usr/local/bin/celery", "-A", "neopsapp", "beat",
              "--scheduler", "neops.enterprise.celery.cron.scheduler:NeopsCeleryScheduler", "-l", "INFO"]
    depends_on:
      cms:
        condition: service_healthy

  engine:
    image: *engine-image
    environment:
      NODE_ENV: production
      PORT: "3030"
      LOG_FORMAT: json
      NEOPS_CMS_URL: http://cms:8000/graphql
      POSTGRES_HOST: postgres-engine
      POSTGRES_PORT: "5432"
      POSTGRES_PASSWORD: ${NEOPS_ENGINE_DB_PASSWORD}
      NEOPS_AUTHZ_MODE: enforce
      NEOPS_JWT_PUBLIC_KEY_PATH: /etc/neops/jwt/public.pem
      NEOPS_CORS_ORIGINS: ${NEOPS_WEB_URL},${NEOPS_WORKFLOWS_URL}
    env_file:
      # NEOPS_CMS_TOKEN, minted by ./neops token after the CMS is up. Absent on the very first start.
      - path: ${NEOPS_DATA_DIR:-./data}/secrets/engine.env
        required: false
    volumes:
      - ${NEOPS_DATA_DIR:-./data}/secrets/jwt/public.pem:/etc/neops/jwt/public.pem:ro
    depends_on:
      postgres-engine:
        condition: service_healthy
      cms:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "node -e \"require('http').get('http://localhost:3030/health', r => process.exit(r.statusCode === 200 ? 0 : 1)).on('error', () => process.exit(1))\""]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 60s
    logging: *logging
    restart: unless-stopped

  monitor:
    image: *monitor-image
    environment:
      ENGINE_BASE_URL: ${NEOPS_ENGINE_URL}
      WEBCLIENT_ORIGIN: ${NEOPS_WEB_URL}
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost/config.js >/dev/null || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 6
    logging: *logging
    restart: unless-stopped

  worker:
    image: *worker-image
    environment:
      URL_BLACKBOARD: http://engine:3030
      DIR_FUNCTION_BLOCKS: neops/fb
      WORKER_NAME: ${NEOPS_WORKER_NAME:-neops-worker}
    depends_on:
      engine:
        condition: service_healthy
    logging: *logging
    restart: unless-stopped

  web:
    image: *web-image
    environment:
      FRONTEND_GRAPHQL_ENDPOINT: ${NEOPS_CMS_URL}/graphql
      FRONTEND_FILEAPI_ENDPOINT: ${NEOPS_CMS_URL}/
      FRONTEND_WORKFLOW_MANAGER_URL: ${NEOPS_WORKFLOWS_URL}
      FRONTEND_DEVMODE: "false"
    depends_on:
      cms:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:8080/ >/dev/null || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 6
      start_period: 10s
    logging: *logging
    restart: unless-stopped
```

- [ ] **Step 3: Validate the file renders**

```bash
cat > /tmp/neops-check.env <<'EOF'
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_CMS_DB_PASSWORD=x
NEOPS_ENGINE_DB_PASSWORD=x
DJANGO_SECRET_KEY=x
NEOPS_ADMIN_PASSWORD=x
EOF
mkdir -p generated && touch generated/cms.env
docker compose --env-file /tmp/neops-check.env -f compose.yaml config --quiet && echo CONFIG-OK
docker compose --env-file /tmp/neops-check.env -f compose.yaml config | grep -A3 "cms-worker:" | head -5
```
Expected: `CONFIG-OK`; the `cms-worker` environment shows both `DATABASE_URL` and `ELASTICSEARCH_DSL_SIGNAL_PROCESSOR` (the YAML merge worked).

- [ ] **Step 4: Run the tests** → `uv run pytest tests/unit/test_compose_files.py -q` → `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add compose.yaml tests/unit/test_compose_files.py
git commit -qm "feat(compose): the 2.0 base stack with bind-mount-only state"
```

---

## Task 9: Routing overlays (`expose`, `traefik`, `shared-host`, `tls-files`, `tls-acme`)

**Files:**
- Create/overwrite: `compose.expose.yaml`, `compose.traefik.yaml`, `compose.traefik-shared-host.yaml`, `compose.tls-files.yaml`, `compose.tls-acme.yaml`

- [ ] **Step 1: Write `compose.expose.yaml`**

```yaml
# Overlay: no bundled proxy. Publishes each browser-facing service on the host, bound to
# 127.0.0.1 by default, for a reverse proxy the operator runs on this host.
# Proxy contract (see README "External reverse proxy"): route by hostname, terminate TLS,
# OVERWRITE X-Forwarded-Proto and X-Real-IP, deny the engine's public worker routes,
# allow request bodies of 200 MB towards the CMS.
services:
  web:
    ports:
      - "${NEOPS_BIND_ADDRESS:-127.0.0.1}:${NEOPS_WEB_PORT:-8080}:8080"
  cms:
    ports:
      - "${NEOPS_BIND_ADDRESS:-127.0.0.1}:${NEOPS_CMS_PORT:-8000}:8000"
  engine:
    ports:
      - "${NEOPS_BIND_ADDRESS:-127.0.0.1}:${NEOPS_ENGINE_PORT:-3030}:3030"
  monitor:
    ports:
      - "${NEOPS_BIND_ADDRESS:-127.0.0.1}:${NEOPS_MONITOR_PORT:-3031}:80"
```

- [ ] **Step 2: Write `compose.traefik.yaml`**

```yaml
# Overlay: bundled Traefik v3, file provider (no Docker socket). Routing is rendered by
# ./neops render into generated/traefik/ from the NEOPS_*_URL values in .env.
x-ratelimit: &ratelimit
  # Traefik sets and overwrites X-Real-Ip, so core's per-IP rate limits see the client address.
  RATELIMIT_IP_META_KEY: HTTP_X_REAL_IP

services:
  traefik:
    image: traefik:v3.6.25
    command: ["--configFile=/etc/traefik/traefik.yml"]
    ports:
      - "${NEOPS_HTTP_PORT:-80}:80"
      - "${NEOPS_HTTPS_PORT:-443}:443"
    volumes:
      - ./generated/traefik/traefik.yml:/etc/traefik/traefik.yml:ro
      - ./generated/traefik/dynamic.yml:/etc/traefik/dynamic.yml:ro
    depends_on:
      web:
        condition: service_healthy
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "5"
    restart: unless-stopped

  cms-init:
    environment: *ratelimit
  cms:
    environment: *ratelimit
  cms-worker:
    environment: *ratelimit
  cms-beat:
    environment: *ratelimit
```

- [ ] **Step 3: Write `compose.traefik-shared-host.yaml`**

```yaml
# Overlay: single public hostname. Core's URL prefixes are routed to the CMS on the web
# client's hostname (rendered from NEOPS_CMS_URL == NEOPS_WEB_URL); the engine and Keycloak
# live under path prefixes; the monitor app gets its own entrypoint so it stays a
# distinct origin (NEOPS_WORKFLOWS_URL=https://<host>:<NEOPS_MONITOR_PORT>).
services:
  traefik:
    ports:
      - "${NEOPS_MONITOR_PORT:-8443}:8443"
```

- [ ] **Step 4: Write `compose.tls-files.yaml`**

```yaml
# Overlay: TLS from PEM files. Either operator-provided (./certs/cert.pem, ./certs/key.pem, a
# wildcard or SAN certificate covering every hostname in .env) or self-signed by ./neops keys
# when NEOPS_TLS_SELF_SIGNED=true (then point the two variables at data/secrets/tls/).
services:
  traefik:
    volumes:
      - ${NEOPS_TLS_CERT_FILE:-./certs/cert.pem}:/etc/traefik/certs/cert.pem:ro
      - ${NEOPS_TLS_KEY_FILE:-./certs/key.pem}:/etc/traefik/certs/key.pem:ro
```

- [ ] **Step 5: Write `compose.tls-acme.yaml`**

```yaml
# Overlay: TLS from Let's Encrypt (HTTP-01). Needs public DNS for every hostname and
# NEOPS_HTTP_PORT=80 reachable from the internet. Certificates persist in data/traefik/acme.
services:
  traefik:
    volumes:
      - ${NEOPS_DATA_DIR:-./data}/traefik/acme:/acme
```

- [ ] **Step 6: Validate merges**

```bash
mkdir -p generated/traefik && touch generated/traefik/traefik.yml generated/traefik/dynamic.yml certs/cert.pem certs/key.pem
docker compose --env-file /tmp/neops-check.env -f compose.yaml -f compose.traefik.yaml -f compose.traefik-shared-host.yaml -f compose.tls-files.yaml config | grep -A8 "^  traefik:" | grep -c "published"
```
Expected: `3` (80, 443 and 8443 all survive the merge). Then
`docker compose --env-file /tmp/neops-check.env -f compose.yaml -f compose.traefik.yaml config | grep -B2 -A1 RATELIMIT | head` shows the key on the cms services alongside `DATABASE_URL` (map merge, nothing lost). Remove the placeholder cert files afterwards: `rm certs/cert.pem certs/key.pem`.

- [ ] **Step 7: Run the invariant tests and commit**

```bash
uv run pytest tests/unit/test_compose_files.py -q
git add compose.expose.yaml compose.traefik.yaml compose.traefik-shared-host.yaml compose.tls-files.yaml compose.tls-acme.yaml
git commit -qm "feat(compose): routing overlays (expose, traefik, shared-host, tls-files, tls-acme)"
```

---

## Task 10: Auth overlays (`oidc`, `keycloak`)

**Files:**
- Create/overwrite: `compose.oidc.yaml`, `compose.keycloak.yaml`

- [ ] **Step 1: Write `compose.oidc.yaml`**

```yaml
# Overlay: OIDC login through core's allauth plugin. Providers are seeded from
# generated/providers.json (rendered from NEOPS_OIDC_* or from the Keycloak overlay).
# Local username/password login is disabled while this overlay is active; the static
# API key the engine uses keeps working.
x-oidc-env: &oidc-env
  NEOPS_PLUGINS: neops_cron neops_auth_static_api_key neops_auth_allauth neops_permissions_simple neops_reports
  AUTH_PROVIDERS_CONFIG_PATH: /etc/neops/providers.json
  NEOPS_FRONTEND_CALLBACK_URL: ${NEOPS_WEB_URL}/auth/callback
  NEOPS_FRONTEND_LOGIN_URL: ${NEOPS_WEB_URL}/login

x-oidc-volumes: &oidc-volumes
  - ./generated/providers.json:/etc/neops/providers.json:ro

services:
  cms-init:
    environment: *oidc-env
    volumes: *oidc-volumes
  cms:
    environment: *oidc-env
    volumes: *oidc-volumes
  cms-worker:
    environment: *oidc-env
    volumes: *oidc-volumes
  cms-beat:
    environment: *oidc-env
    volumes: *oidc-volumes
```

- [ ] **Step 2: Write `compose.keycloak.yaml`**

```yaml
# Overlay: bundled Keycloak 26 as the OIDC provider (requires compose.oidc.yaml).
# The realm `neops` with the confidential client `neops-auth` is imported from
# generated/keycloak/realm.json on the FIRST start only; later changes are made in the
# admin console at NEOPS_KEYCLOAK_URL (user admin, password NEOPS_KEYCLOAK_ADMIN_PASSWORD).
# The CMS talks to Keycloak in-network (http://keycloak:8080); browsers use NEOPS_KEYCLOAK_URL,
# which KC_HOSTNAME_BACKCHANNEL_DYNAMIC reconciles.
services:
  postgres-keycloak:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: keycloak
      POSTGRES_DB: keycloak
      POSTGRES_PASSWORD: ${NEOPS_KEYCLOAK_DB_PASSWORD}
    volumes:
      - ${NEOPS_DATA_DIR:-./data}/keycloak/postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U keycloak -d keycloak"]
      interval: 10s
      timeout: 5s
      retries: 6
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "5"
    restart: unless-stopped

  keycloak:
    image: quay.io/keycloak/keycloak:26.5.2
    command: ["start", "--import-realm"]
    environment:
      KC_DB: postgres
      KC_DB_URL: jdbc:postgresql://postgres-keycloak:5432/keycloak
      KC_DB_USERNAME: keycloak
      KC_DB_PASSWORD: ${NEOPS_KEYCLOAK_DB_PASSWORD}
      KC_HOSTNAME: ${NEOPS_KEYCLOAK_URL}
      KC_HOSTNAME_BACKCHANNEL_DYNAMIC: "true"
      KC_HTTP_ENABLED: "true"
      KC_PROXY_HEADERS: xforwarded
      KC_HEALTH_ENABLED: "true"
      KC_HTTP_MANAGEMENT_RELATIVE_PATH: /
      KC_BOOTSTRAP_ADMIN_USERNAME: admin
      KC_BOOTSTRAP_ADMIN_PASSWORD: ${NEOPS_KEYCLOAK_ADMIN_PASSWORD}
    env_file:
      - ./generated/keycloak.env
    volumes:
      - ./generated/keycloak/realm.json:/opt/keycloak/data/import/neops-realm.json:ro
    ports:
      # Loopback only: for the operator's kcadm/curl and for an external proxy in expose mode.
      - "${NEOPS_BIND_ADDRESS:-127.0.0.1}:${NEOPS_KEYCLOAK_PORT:-8180}:8080"
    depends_on:
      postgres-keycloak:
        condition: service_healthy
    healthcheck:
      # The image ships no curl; this is the form the Keycloak docs give.
      test: ["CMD-SHELL", "exec 3<>/dev/tcp/localhost/9000 && printf 'HEAD /health/ready HTTP/1.0\\r\\n\\r\\n' >&3 && head -1 <&3 | grep -q ' 200 '"]
      interval: 15s
      timeout: 10s
      retries: 8
      start_period: 120s
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "5"
    restart: unless-stopped

  cms-init:
    depends_on:
      keycloak:
        condition: service_healthy
```

- [ ] **Step 3: Validate**

```bash
mkdir -p generated/keycloak && touch generated/providers.json generated/keycloak.env generated/keycloak/realm.json
cat >> /tmp/neops-check.env <<'EOF'
NEOPS_KEYCLOAK_URL=https://auth.neops.example.com
NEOPS_KEYCLOAK_ADMIN_PASSWORD=x
NEOPS_KEYCLOAK_DB_PASSWORD=x
EOF
docker compose --env-file /tmp/neops-check.env -f compose.yaml -f compose.traefik.yaml -f compose.oidc.yaml -f compose.keycloak.yaml config --quiet && echo CONFIG-OK
docker compose --env-file /tmp/neops-check.env -f compose.yaml -f compose.traefik.yaml -f compose.oidc.yaml -f compose.keycloak.yaml config | grep -c "providers.json"
```
Expected: `CONFIG-OK` and `4` (the providers file is mounted in all four core services) and the `cms` volumes list still contains the jwt, cust-cert, media and tmp mounts (lists append).

- [ ] **Step 4: Commit**

```bash
uv run pytest tests/unit/test_compose_files.py -q
git add compose.oidc.yaml compose.keycloak.yaml
git commit -qm "feat(compose): OIDC and bundled Keycloak overlays"
```

---

## Task 11: Metrics overlay

**Files:**
- Create/overwrite: `compose.metrics.yaml`
- Modify: `metrics/scrape_config.yml`, `metrics/README.md`
- Delete: nothing (`metrics/grafana`, `metrics/vmalert`, `metrics/fetch-dashboards.sh` are kept as they are)

- [ ] **Step 1: Write `compose.metrics.yaml`**

```yaml
# Overlay: VictoriaMetrics + vmalert + Grafana + exporters, ported from the 1.0 layout.
# Grafana is the only service routed publicly, and only when NEOPS_GRAFANA_URL is set.
x-logging: &logging
  driver: json-file
  options:
    max-size: "20m"
    max-file: "5"

services:
  celery-exporter:
    image: danihodovic/celery-exporter:0.11.3
    command: ["--broker-url=redis://redis:6379/0"]
    depends_on:
      redis:
        condition: service_healthy
    logging: *logging
    restart: unless-stopped

  redis-exporter:
    image: oliver006/redis_exporter:v1.67.0
    environment:
      REDIS_ADDR: redis://redis:6379
    logging: *logging
    restart: unless-stopped

  postgres-exporter-cms:
    image: prometheuscommunity/postgres-exporter:v0.17.1
    environment:
      DATA_SOURCE_NAME: postgresql://neops:${NEOPS_CMS_DB_PASSWORD}@postgres-cms:5432/neops?sslmode=disable
    logging: *logging
    restart: unless-stopped

  postgres-exporter-engine:
    image: prometheuscommunity/postgres-exporter:v0.17.1
    environment:
      DATA_SOURCE_NAME: postgresql://postgres:${NEOPS_ENGINE_DB_PASSWORD}@postgres-engine:5432/neops-workflow?sslmode=disable
    logging: *logging
    restart: unless-stopped

  elasticsearch-exporter:
    image: prometheuscommunity/elasticsearch-exporter:v1.9.0
    command: ["--es.uri=http://elasticsearch:9200", "--es.all", "--es.indices"]
    logging: *logging
    restart: unless-stopped

  victoriametrics:
    image: victoriametrics/victoria-metrics:v1.126.0
    command:
      - "--promscrape.config=/etc/victoriametrics/scrape_config.yml"
      - "--retentionPeriod=${NEOPS_METRICS_RETENTION:-30d}"
      - "--storageDataPath=/victoria-metrics-data"
    volumes:
      - ./metrics/scrape_config.yml:/etc/victoriametrics/scrape_config.yml:ro
      - ${NEOPS_DATA_DIR:-./data}/metrics/victoria:/victoria-metrics-data
    logging: *logging
    restart: unless-stopped

  vmalert:
    image: victoriametrics/vmalert:v1.126.0
    command:
      - "--datasource.url=http://victoriametrics:8428"
      - "--remoteWrite.url=http://victoriametrics:8428"
      - "--remoteRead.url=http://victoriametrics:8428"
      - "--rule=/etc/vmalert/rules/*.yml"
      - "--evaluationInterval=1m"
      - "--notifier.blackhole=true"
    volumes:
      - ./metrics/vmalert/rules:/etc/vmalert/rules:ro
    depends_on:
      - victoriametrics
    logging: *logging
    restart: unless-stopped

  grafana:
    image: grafana/grafana:11.5.2
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${NEOPS_GRAFANA_ADMIN_PASSWORD}
      GF_USERS_ALLOW_SIGN_UP: "false"
      GF_SERVER_ROOT_URL: ${NEOPS_GRAFANA_URL:-http://localhost:3000}
      GF_SERVER_SERVE_FROM_SUB_PATH: "true"
    volumes:
      - ./metrics/grafana/provisioning:/etc/grafana/provisioning:ro
      - ${NEOPS_DATA_DIR:-./data}/metrics/grafana:/var/lib/grafana
    ports:
      - "${NEOPS_BIND_ADDRESS:-127.0.0.1}:${NEOPS_GRAFANA_PORT:-3000}:3000"
    depends_on:
      - victoriametrics
    logging: *logging
    restart: unless-stopped
```

Pin note: the exporter versions above are the newest stable tags at the time of writing; verify each resolves with `docker manifest inspect <ref>` before committing and adjust to the latest patch if one is missing.

- [ ] **Step 2: Repoint `metrics/scrape_config.yml`**

```yaml
global:
  scrape_interval: 30s

scrape_configs:
  - job_name: neops
    metrics_path: /metrics
    static_configs:
      - targets: [cms:8000]

  - job_name: celery
    static_configs:
      - targets: [celery-exporter:9808]

  - job_name: redis
    static_configs:
      - targets: [redis-exporter:9121]

  - job_name: postgres
    static_configs:
      - targets: [postgres-exporter-cms:9187]
        labels: {database: cms}
      - targets: [postgres-exporter-engine:9187]
        labels: {database: engine}

  - job_name: elasticsearch
    static_configs:
      - targets: [elasticsearch-exporter:9114]

  - job_name: victoriametrics
    static_configs:
      - targets: [victoriametrics:8428]

  - job_name: vmalert
    static_configs:
      - targets: [vmalert:8880]
```

- [ ] **Step 3: Rewrite `metrics/README.md`** (replace the *Usage* section; keep *Services* and *Exporters* and the dashboards notes)

```markdown
## Usage

Add `compose.metrics.yaml` to `COMPOSE_FILE` in `.env` (see `examples/metrics.env`) and run `./neops up`.
Grafana is reachable on the host at `127.0.0.1:${NEOPS_GRAFANA_PORT:-3000}` and, when
`NEOPS_GRAFANA_URL` is set with the Traefik overlay, at that public URL. The admin password is
`NEOPS_GRAFANA_ADMIN_PASSWORD`. Metrics data lives in `data/metrics/` (bind mounts, like everything else).
```

- [ ] **Step 4: Validate and commit**

```bash
echo "NEOPS_GRAFANA_ADMIN_PASSWORD=x" >> /tmp/neops-check.env
docker compose --env-file /tmp/neops-check.env -f compose.yaml -f compose.traefik.yaml -f compose.metrics.yaml config --quiet && echo CONFIG-OK
uv run pytest tests/unit/test_compose_files.py -q
git add compose.metrics.yaml metrics/scrape_config.yml metrics/README.md
git commit -qm "feat(compose): port the metrics stack as an overlay"
```

---

## Task 12: `.env.example` and `examples/*.env`

**Files:**
- Create: `.env.example`, `examples/external-proxy.env`, `examples/traefik-http.env`, `examples/traefik-tls-files.env`, `examples/traefik-tls-selfsigned.env`, `examples/traefik-acme.env`, `examples/traefik-shared-host-tls-files.env`, `examples/oidc-external.env`, `examples/oidc-keycloak.env`, `examples/metrics.env`
- Test: `tests/unit/test_examples.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_examples.py
from pathlib import Path

from neops_compose.env import Env
from neops_compose.rules import problems
from neops_compose.scenario import Scenario

REPO = Path(__file__).resolve().parents[2]
SECRET_KEYS = ("NEOPS_CMS_DB_PASSWORD", "NEOPS_ENGINE_DB_PASSWORD", "DJANGO_SECRET_KEY", "NEOPS_ADMIN_PASSWORD",
               "NEOPS_KEYCLOAK_ADMIN_PASSWORD", "NEOPS_KEYCLOAK_DB_PASSWORD", "NEOPS_GRAFANA_ADMIN_PASSWORD",
               "NEOPS_OIDC_CLIENT_ID", "NEOPS_OIDC_CLIENT_SECRET")


def filled(example: Path, tmp_path: Path) -> Env:
    """An example with every secret filled with a dummy value, as an operator would."""
    text = example.read_text()
    for key in SECRET_KEYS:
        text = text.replace(f"\n{key}=\n", f"\n{key}=dummy{key.lower()}0123456789\n")
    target = tmp_path / ".env"
    target.write_text(text)
    return Env(target)


def test_every_example_validates_once_secrets_are_filled(tmp_path):
    examples = sorted((REPO / "examples").glob("*.env"))
    assert len(examples) == 9
    for example in examples:
        env = filled(example, tmp_path)
        assert problems(env, Scenario.from_env(env), REPO) == [], example.name


def test_examples_ship_no_secrets():
    for example in [REPO / ".env.example", *(REPO / "examples").glob("*.env")]:
        for line in example.read_text().splitlines():
            for key in SECRET_KEYS:
                if line.startswith(key + "="):
                    assert line == key + "=", f"{example.name}: {key} must ship empty"
```

- [ ] **Step 2: Write `.env.example`** (the annotated reference of every key)

```bash
# =============================================================================
# Neops 2.0 docker-compose — configuration reference
# =============================================================================
# Copy one of examples/*.env to .env, fill in the secrets and URLs, then run:
#     ./neops install
# Every key below is documented once. Keys with a default may be omitted.
# Secrets must be filled (never leave them blank or as "changeme"); generate with:
#     openssl rand -hex 32

# --- Scenario ----------------------------------------------------------------
# The compose files that make up your deployment. compose.yaml is always first.
#   compose.expose.yaml                bind ports on 127.0.0.1 for your own reverse proxy
#   compose.traefik.yaml               bundled Traefik, one hostname per service
#   compose.traefik-shared-host.yaml   + everything on one hostname (with compose.traefik.yaml)
#   compose.tls-files.yaml             + TLS from PEM files (or self-signed, see below)
#   compose.tls-acme.yaml              + TLS from Let's Encrypt (needs NEOPS_HTTP_PORT=80)
#   compose.oidc.yaml                  OIDC login (allauth); disables local password login
#   compose.keycloak.yaml              + bundled Keycloak as the OIDC provider
#   compose.metrics.yaml               VictoriaMetrics / Grafana / exporters
#   compose.override.yaml              your own local additions (gitignored)
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml

# --- Public URLs (browser-facing) ---------------------------------------------
# Hostname-per-service (default): four hostnames. Shared-hostname mode: NEOPS_CMS_URL equals
# NEOPS_WEB_URL, the engine gets a path, the monitor gets the port NEOPS_MONITOR_PORT.
# Rules: NEOPS_CMS_URL never has a path; NEOPS_WORKFLOWS_URL must be a different origin than
# NEOPS_WEB_URL; with a TLS overlay all URLs are https://, without one http://.
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
# Keycloak overlay only:
# NEOPS_KEYCLOAK_URL=https://auth.neops.example.com
# Metrics overlay only (optional; leave unset to keep Grafana on 127.0.0.1):
# NEOPS_GRAFANA_URL=https://grafana.neops.example.com

# --- Secrets (required) -------------------------------------------------------
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
# The first CMS superuser. The password is applied when the user is created; change it
# later with ./neops rotate admin-password.
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=

# --- Ports (bundled Traefik) --------------------------------------------------
# NEOPS_HTTP_PORT=80
# NEOPS_HTTPS_PORT=443
# NEOPS_MONITOR_PORT=8443          # shared-hostname mode only

# --- Ports (external proxy, compose.expose.yaml) ------------------------------
# NEOPS_BIND_ADDRESS=127.0.0.1
# NEOPS_WEB_PORT=8080
# NEOPS_CMS_PORT=8000
# NEOPS_ENGINE_PORT=3030
# NEOPS_MONITOR_PORT=3031
# NEOPS_KEYCLOAK_PORT=8180
# NEOPS_GRAFANA_PORT=3000
# Only with a proxy that OVERWRITES X-Real-IP (Traefik does; nginx needs proxy_set_header).
# Without it every login fails with a 500 (django-ratelimit).
# RATELIMIT_IP_META_KEY=HTTP_X_REAL_IP

# --- TLS (compose.tls-files.yaml) ---------------------------------------------
NEOPS_TLS_CERT_FILE=./certs/cert.pem
NEOPS_TLS_KEY_FILE=./certs/key.pem
# Self-signed instead (evaluation): ./neops keys mints a CA + SAN certificate.
# NEOPS_TLS_SELF_SIGNED=true
# NEOPS_TLS_CERT_FILE=./data/secrets/tls/cert.pem
# NEOPS_TLS_KEY_FILE=./data/secrets/tls/key.pem

# --- TLS (compose.tls-acme.yaml) ----------------------------------------------
# NEOPS_ACME_EMAIL=ops@example.com

# --- OIDC with an external identity provider (compose.oidc.yaml) --------------
# NEOPS_OIDC_PROVIDER_ID=entra          # short id; part of the callback URL
# NEOPS_OIDC_NAME=Company SSO           # label on the login button
# NEOPS_OIDC_CLIENT_ID=
# NEOPS_OIDC_CLIENT_SECRET=
# NEOPS_OIDC_DISCOVERY_URL=https://login.example.com/.well-known/openid-configuration
# Register this redirect URI at the provider:
#   <NEOPS_CMS_URL>/accounts/oidc/<NEOPS_OIDC_PROVIDER_ID>/login/callback/

# --- Bundled Keycloak (compose.keycloak.yaml, requires compose.oidc.yaml) -----
# NEOPS_KEYCLOAK_ADMIN_PASSWORD=
# NEOPS_KEYCLOAK_DB_PASSWORD=

# --- Metrics (compose.metrics.yaml) -------------------------------------------
# NEOPS_GRAFANA_ADMIN_PASSWORD=
# NEOPS_METRICS_RETENTION=30d

# --- Sizing --------------------------------------------------------------------
# NEOPS_ES_HEAP=1g
# NEOPS_CMS_WORKER_CONCURRENCY=2
# NEOPS_WORKER_NAME=neops-worker

# --- Data location -------------------------------------------------------------
# Every durable byte lives here (bind mounts). Backup = .env + this directory.
# NEOPS_DATA_DIR=./data

# --- Core settings passed through unchanged (see the core documentation) --------
# SENTRY_DSN=
# SENTRY_ENVIRONMENT=
# EMAIL_URL=smtp+tls://user:pass@mail.example.com:587
# NEOPS_LOGGER_CLASS=neops.core.log.loggers.db.DbLogger
# EXECUTION_LOG_RETENTION_ENABLED=False
# EXECUTION_LOG_RETENTION_DAYS=90
# FACT_CLEANUP_ENABLED=False
# NEOPS_EXECUTION_CLEANUP_ENABLED=False
# NORNIR_THREADS=50
# NORNIR_CLOSE_CONNECTION=True
# NORNIR_CONNECTION_DELAY_FACTOR=2

# --- Image overrides (emergencies only; the repo pins a tested set) --------------
# NEOPS_CORE_TAG=  NEOPS_ENGINE_TAG=  NEOPS_MONITOR_TAG=  NEOPS_WORKER_TAG=  NEOPS_WEB_TAG=
# NEOPS_CORE_IMAGE=  NEOPS_ENGINE_IMAGE=  NEOPS_MONITOR_IMAGE=  NEOPS_WORKER_IMAGE=  NEOPS_WEB_IMAGE=
```

- [ ] **Step 3: Write the nine examples**

Each example is complete: an operator copies it to `.env`, fills the blank secrets, and edits the hostnames. Shared header for all nine (write it verbatim at the top of each file, then the scenario block):

```bash
# Neops 2.0 — <scenario name>. Copy to .env, fill every blank secret (openssl rand -hex 32),
# set your hostnames, then: ./neops install.  Reference for every key: .env.example
COMPOSE_PATH_SEPARATOR=:
```

`examples/external-proxy.env`:
```bash
COMPOSE_FILE=compose.yaml:compose.expose.yaml
# Your reverse proxy terminates TLS and forwards to 127.0.0.1:<port>: web 8080, cms 8000,
# engine 3030, monitor 3031. It must OVERWRITE X-Forwarded-Proto and X-Real-IP, allow 200 MB
# bodies to the CMS, and DENY these engine routes (all POST): /blackboard/job, /blackboard/job/*,
# /workers/register, /workers/*/ping, /workers/*/unregister, /function-blocks/register.
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_BIND_ADDRESS=127.0.0.1
# Uncomment once your proxy sets X-Real-IP (see .env.example):
# RATELIMIT_IP_META_KEY=HTTP_X_REAL_IP
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=
```

`examples/traefik-http.env`:
```bash
COMPOSE_FILE=compose.yaml:compose.traefik.yaml
# Plain HTTP on port NEOPS_HTTP_PORT. Evaluation only.
NEOPS_WEB_URL=http://neops.example.com
NEOPS_CMS_URL=http://cms.neops.example.com
NEOPS_ENGINE_URL=http://engine.neops.example.com
NEOPS_WORKFLOWS_URL=http://workflows.neops.example.com
NEOPS_HTTP_PORT=80
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=
```

`examples/traefik-tls-files.env`:
```bash
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml
# Put a certificate covering all four hostnames (wildcard or SAN) in ./certs/cert.pem and its
# key in ./certs/key.pem. HTTP on NEOPS_HTTP_PORT redirects to HTTPS on NEOPS_HTTPS_PORT.
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_HTTP_PORT=80
NEOPS_HTTPS_PORT=443
NEOPS_TLS_CERT_FILE=./certs/cert.pem
NEOPS_TLS_KEY_FILE=./certs/key.pem
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=
```

`examples/traefik-tls-selfsigned.env`:
```bash
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml
# ./neops keys mints a private CA and a certificate for all four hostnames under
# data/secrets/tls/. Import data/secrets/tls/ca.pem into your browsers, or accept the warning.
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_HTTP_PORT=80
NEOPS_HTTPS_PORT=443
NEOPS_TLS_SELF_SIGNED=true
NEOPS_TLS_CERT_FILE=./data/secrets/tls/cert.pem
NEOPS_TLS_KEY_FILE=./data/secrets/tls/key.pem
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=
```

`examples/traefik-acme.env`:
```bash
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-acme.yaml
# Let's Encrypt (HTTP-01): every hostname must resolve publicly to this host, and port 80
# must be reachable from the internet. Certificates are stored in data/traefik/acme/.
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_HTTP_PORT=80
NEOPS_HTTPS_PORT=443
NEOPS_ACME_EMAIL=ops@example.com
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=
```

`examples/traefik-shared-host-tls-files.env`:
```bash
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.traefik-shared-host.yaml:compose.tls-files.yaml
# One hostname. Core answers under its own URL prefixes (/graphql, /admin, /djstatic, ...) on the
# web client's hostname, the engine under /engine, the monitor on port NEOPS_MONITOR_PORT.
# The certificate in ./certs/ needs only this one hostname.
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://neops.example.com
NEOPS_ENGINE_URL=https://neops.example.com/engine
NEOPS_WORKFLOWS_URL=https://neops.example.com:8443
NEOPS_HTTP_PORT=80
NEOPS_HTTPS_PORT=443
NEOPS_MONITOR_PORT=8443
NEOPS_TLS_CERT_FILE=./certs/cert.pem
NEOPS_TLS_KEY_FILE=./certs/key.pem
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=
```

`examples/oidc-external.env`:
```bash
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml:compose.oidc.yaml
# Login through your identity provider. Register the redirect URI
#   <NEOPS_CMS_URL>/accounts/oidc/<NEOPS_OIDC_PROVIDER_ID>/login/callback/
# and make sure the ID token (or userinfo) carries resource_access.<client>.roles.
# Local password login is disabled; the first user to log in gets no roles until assigned.
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_HTTP_PORT=80
NEOPS_HTTPS_PORT=443
NEOPS_TLS_CERT_FILE=./certs/cert.pem
NEOPS_TLS_KEY_FILE=./certs/key.pem
NEOPS_OIDC_PROVIDER_ID=entra
NEOPS_OIDC_NAME=Company SSO
NEOPS_OIDC_CLIENT_ID=
NEOPS_OIDC_CLIENT_SECRET=
NEOPS_OIDC_DISCOVERY_URL=https://login.example.com/.well-known/openid-configuration
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=
```

`examples/oidc-keycloak.env`:
```bash
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml:compose.oidc.yaml:compose.keycloak.yaml
# Bundled Keycloak at NEOPS_KEYCLOAK_URL (admin console: user admin). The realm `neops` and the
# client `neops-auth` are created on first start; add users and assign them client roles there.
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_KEYCLOAK_URL=https://auth.neops.example.com
NEOPS_HTTP_PORT=80
NEOPS_HTTPS_PORT=443
NEOPS_TLS_CERT_FILE=./certs/cert.pem
NEOPS_TLS_KEY_FILE=./certs/key.pem
NEOPS_KEYCLOAK_ADMIN_PASSWORD=
NEOPS_KEYCLOAK_DB_PASSWORD=
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=
```

`examples/metrics.env`:
```bash
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml:compose.metrics.yaml
# Adds VictoriaMetrics, vmalert, Grafana and the exporters. Grafana is public at
# NEOPS_GRAFANA_URL (remove that line to keep it on 127.0.0.1:3000 only).
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_GRAFANA_URL=https://grafana.neops.example.com
NEOPS_HTTP_PORT=80
NEOPS_HTTPS_PORT=443
NEOPS_TLS_CERT_FILE=./certs/cert.pem
NEOPS_TLS_KEY_FILE=./certs/key.pem
NEOPS_GRAFANA_ADMIN_PASSWORD=
NEOPS_CMS_DB_PASSWORD=
NEOPS_ENGINE_DB_PASSWORD=
DJANGO_SECRET_KEY=
NEOPS_ADMIN_USER=neops
NEOPS_ADMIN_EMAIL=neops@example.com
NEOPS_ADMIN_PASSWORD=
```

- [ ] **Step 4: Run the tests and commit**

```bash
uv run pytest tests/unit/test_examples.py -q      # 2 passed
git add .env.example examples
git commit -qm "docs: configuration reference and one example .env per scenario"
```

---
## Task 13: `compose.py` and `state.py`

**Files:**
- Create: `neops_compose/compose.py`, `neops_compose/state.py`
- Test: `tests/unit/test_state.py`, `tests/unit/test_compose.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_state.py
import stat

from neops_compose.state import State


def test_missing_state_is_empty(tmp_path):
    s = State.load(tmp_path / "x" / "state.json")
    assert s.applied == [] and s.faked == [] and s.api_keys == [] and s.last_up is None


def test_save_creates_parent_and_is_private(tmp_path):
    p = tmp_path / ".neops" / "state.json"
    s = State.load(p)
    s.record_applied("0001_initial_layout")
    s.record_api_key(7, "workflow")
    s.record_up({"cms": "quay.io/zebbra/neops-core:2.1.0-beta.5"})
    s.save(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    again = State.load(p)
    assert again.applied_names == ["0001_initial_layout"]
    assert again.api_keys[0]["id"] == 7 and again.last_up["images"]["cms"].endswith("beta.5")
    assert again.cli == "2.0.0" and again.schema == 1


def test_newer_cli_is_detected(tmp_path):
    p = tmp_path / "state.json"
    s = State.load(p)
    s.cli = "9.0.0"
    s.save(p)
    assert State.load(p).written_by_newer_cli() is True
    s.cli = "1.0.0"
    s.save(p)
    assert State.load(p).written_by_newer_cli() is False
```

```python
# tests/unit/test_compose.py
from pathlib import Path

from neops_compose.compose import Compose


def test_compose_runs_docker_compose_from_the_repo_root(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class R:
            returncode = 0
            stdout = '{"Service":"cms","State":"running","Health":"healthy"}\n{"Service":"web","State":"exited","Health":""}\n'
            stderr = ""
        return R()

    monkeypatch.setattr("neops_compose.compose.subprocess.run", fake_run)
    c = Compose(tmp_path)
    ps = c.ps()
    assert calls[0][0][:3] == ["docker", "compose", "ps"] and calls[0][1]["cwd"] == tmp_path
    assert ps == [{"Service": "cms", "State": "running", "Health": "healthy"},
                  {"Service": "web", "State": "exited", "Health": ""}]
    assert c.running_services() == {"cms"}
    c.up("cms", "engine", force_recreate=True)
    assert calls[-1][0] == ["docker", "compose", "up", "-d", "--wait", "--wait-timeout", "900",
                            "--force-recreate", "cms", "engine"]
    c.exec("cms", "python", "manage.py", "check", env={"X": "1"})
    assert calls[-1][0][:8] == ["docker", "compose", "exec", "-T", "-e", "X=1", "cms", "python"]
```

- [ ] **Step 2: Run to verify failure** → `uv run pytest tests/unit/test_state.py tests/unit/test_compose.py -q` → collection errors.

- [ ] **Step 3: Write `neops_compose/state.py`**

```python
from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from neops_compose import __version__

SCHEMA = 1


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


@dataclass
class State:
    """data/.neops/state.json: what the CLI has done to this installation."""

    schema: int = SCHEMA
    cli: str = __version__
    applied: list[dict] = field(default_factory=list)
    faked: list[dict] = field(default_factory=list)
    api_keys: list[dict] = field(default_factory=list)
    last_up: dict | None = None

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        return cls(schema=raw.get("schema", SCHEMA), cli=raw.get("cli", "0.0.0"), applied=raw.get("applied", []),
                   faked=raw.get("faked", []), api_keys=raw.get("api_keys", []), last_up=raw.get("last_up"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.cli = __version__ if _version_tuple(self.cli) <= _version_tuple(__version__) else self.cli
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    @property
    def applied_names(self) -> list[str]:
        return [m["name"] for m in self.applied]

    @property
    def faked_names(self) -> list[str]:
        return [m["name"] for m in self.faked]

    def record_applied(self, name: str) -> None:
        self.applied.append({"name": name, "at": _now(), "cli": __version__})

    def record_faked(self, name: str) -> None:
        self.faked.append({"name": name, "at": _now(), "cli": __version__})

    def record_api_key(self, key_id: int, app: str) -> None:
        self.api_keys.append({"id": key_id, "app": app, "at": _now()})

    def record_up(self, images: dict[str, str]) -> None:
        self.last_up = {"at": _now(), "images": images}

    def written_by_newer_cli(self) -> bool:
        return _version_tuple(self.cli) > _version_tuple(__version__)
```

- [ ] **Step 4: Write `neops_compose/compose.py`**

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

WAIT_TIMEOUT = 900


class ComposeError(RuntimeError):
    pass


class Compose:
    """docker compose, always run from the repo root so COMPOSE_FILE's relative entries resolve."""

    def __init__(self, repo: Path):
        self.repo = repo

    def run(self, *args: str, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(["docker", "compose", *args], cwd=self.repo, text=True,
                                capture_output=capture)
        if check and result.returncode != 0:
            detail = (result.stderr or "").strip() if capture else ""
            raise ComposeError(f"docker compose {' '.join(args)} failed ({result.returncode}) {detail}".strip())
        return result

    def up(self, *services: str, wait: bool = True, force_recreate: bool = False) -> None:
        args = ["up", "-d"]
        if wait:
            args += ["--wait", "--wait-timeout", str(WAIT_TIMEOUT)]
        if force_recreate:
            args.append("--force-recreate")
        self.run(*args, *services)

    def pull(self) -> None:
        self.run("pull", "--quiet")

    def down(self) -> None:
        self.run("down", "--remove-orphans")

    def exec(self, service: str, *cmd: str, env: dict[str, str] | None = None) -> str:
        flags: list[str] = []
        for key, value in (env or {}).items():
            flags += ["-e", f"{key}={value}"]
        return self.run("exec", "-T", *flags, service, *cmd, capture=True).stdout

    def ps(self) -> list[dict]:
        out = self.run("ps", "-a", "--format", "json", capture=True).stdout.strip()
        if not out:
            return []
        if out.startswith("["):
            return json.loads(out)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def running_services(self) -> set[str]:
        return {row["Service"] for row in self.ps() if row.get("State") == "running"}

    def images(self) -> list[str]:
        out = self.run("config", "--images", capture=True).stdout
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    def service_names(self) -> list[str]:
        out = self.run("config", "--services", capture=True).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]
```

- [ ] **Step 5: Run the tests** → `uv run pytest tests/unit/test_state.py tests/unit/test_compose.py -q` → `4 passed`.

- [ ] **Step 6: Commit**

```bash
git add neops_compose/compose.py neops_compose/state.py tests/unit/test_state.py tests/unit/test_compose.py
git commit -qm "feat(cli): docker compose wrapper and the installation state file"
```

---

## Task 14: `migrate.py` and `0001_initial_layout`

**Files:**
- Create: `neops_compose/migrate.py`, `migrations/0001_initial_layout.py`, `migrations/README.md`
- Test: `tests/unit/test_migrate.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_migrate.py
from pathlib import Path

import pytest

from neops_compose import migrate
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.state import State


def write_migration(d: Path, name: str, body: str) -> None:
    (d / f"{name}.py").write_text(f'DESCRIPTION = "{name}"\nIDEMPOTENT = True\n\n{body}\n')


def make(tmp_path):
    (tmp_path / ".env").write_text("A=1\n")
    env = Env(tmp_path / ".env")
    paths = Paths.for_repo(tmp_path, env)
    (tmp_path / "migrations").mkdir()
    (tmp_path / "backups").mkdir()
    return env, paths


def test_discover_orders_and_validates_names(tmp_path):
    env, paths = make(tmp_path)
    write_migration(paths.migrations, "0002_second", "def apply(ctx): pass")
    write_migration(paths.migrations, "0001_first", "def apply(ctx): pass")
    assert [m.name for m in migrate.discover(paths.migrations)] == ["0001_first", "0002_second"]
    (paths.migrations / "bad_name.py").write_text("def apply(ctx): pass\n")
    with pytest.raises(migrate.MigrationError, match="bad_name"):
        migrate.discover(paths.migrations)


def test_apply_all_runs_pending_in_order_records_state_and_snapshots(tmp_path):
    env, paths = make(tmp_path)
    write_migration(paths.migrations, "0001_a", "def apply(ctx):\n    ctx.mkdir(ctx.data / 'made'); ctx.env.set('B', '2')")
    write_migration(paths.migrations, "0002_b", "def apply(ctx):\n    ctx.log('hello')")
    logs = []
    state = State()
    done = migrate.apply_all(env, paths, state, log=logs.append)
    assert done == ["0001_a", "0002_b"]
    assert (paths.data / "made").is_dir() and Env(paths.env_file).get("B") == "2"
    assert State.load(paths.state_file).applied_names == ["0001_a", "0002_b"]
    snapshots = list(paths.backups.glob("pre-migrate-*"))
    assert len(snapshots) == 1 and (snapshots[0] / ".env").exists()
    assert migrate.apply_all(env, paths, State.load(paths.state_file), log=logs.append) == []


def test_failed_migration_is_not_recorded_and_non_idempotent_blocks_rerun(tmp_path):
    env, paths = make(tmp_path)
    (paths.migrations / "0001_boom.py").write_text('DESCRIPTION = "boom"\nIDEMPOTENT = False\n\ndef apply(ctx):\n    raise RuntimeError("disk full")\n')
    with pytest.raises(migrate.MigrationError, match="0001_boom"):
        migrate.apply_all(env, paths, State(), log=lambda m: None)
    assert State.load(paths.state_file).applied_names == []
    marker = paths.state_file.parent / "0001_boom.failed"
    assert marker.exists()
    with pytest.raises(migrate.MigrationError, match="not idempotent"):
        migrate.apply_all(env, paths, State.load(paths.state_file), log=lambda m: None)


def test_fake_requires_full_name_and_is_recorded_separately(tmp_path):
    env, paths = make(tmp_path)
    write_migration(paths.migrations, "0001_a", "def apply(ctx): raise RuntimeError('never')")
    with pytest.raises(migrate.MigrationError, match="0009_nope"):
        migrate.fake("0009_nope", paths, State())
    migrate.fake("0001_a", paths, State())
    s = State.load(paths.state_file)
    assert s.applied_names == ["0001_a"] and s.faked_names == ["0001_a"]
    assert migrate.apply_all(env, paths, s, log=lambda m: None) == []


def test_initial_layout_creates_the_tree(tmp_path, monkeypatch):
    env, paths = make(tmp_path)
    real = Path(__file__).resolve().parents[2] / "migrations" / "0001_initial_layout.py"
    (paths.migrations / real.name).write_text(real.read_text())
    chowns = []
    monkeypatch.setattr(migrate.Ctx, "chown_via_container", lambda self, p, uid, gid: chowns.append((p, uid, gid)))
    migrate.apply_all(env, paths, State(), log=lambda m: None)
    for d in paths.data_dirs():
        assert d.is_dir(), d
    assert oct(paths.secrets.stat().st_mode & 0o777) == "0o700"
    assert chowns == [(paths.data / "elasticsearch", 1000, 0)]
```

- [ ] **Step 2: Run to verify failure** → collection error.

- [ ] **Step 3: Write `neops_compose/migrate.py`**

```python
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.state import State

NAME_RE = re.compile(r"^\d{4}_[a-z0-9_]+$")
CHOWN_IMAGE = "alpine:3.20"


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    name: str
    description: str
    idempotent: bool
    module: ModuleType

    def apply(self, ctx: Ctx) -> None:
        self.module.apply(ctx)


@dataclass
class Ctx:
    """What a migration may touch. Migrations run before containers start."""

    repo: Path
    data: Path
    env: Env
    log: Callable[[str], None]

    def mkdir(self, path: Path, mode: int | None = None) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if mode is not None:
            os.chmod(path, mode)

    def move(self, src: Path, dst: Path) -> None:
        if not src.exists():
            self.log(f"skip move, {src} does not exist")
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        self.log(f"moved {src} -> {dst}")

    def chown_via_container(self, path: Path, uid: int, gid: int) -> None:
        """chown without sudo: docker runs as root, so a throwaway container can do it."""
        subprocess.run(["docker", "run", "--rm", "-v", f"{path.resolve()}:/target", CHOWN_IMAGE,
                        "chown", "-R", f"{uid}:{gid}", "/target"], check=True)
        self.log(f"chowned {path} to {uid}:{gid}")

    def compose(self, *args: str) -> None:
        subprocess.run(["docker", "compose", *args], cwd=self.repo, check=True)


def discover(migrations_dir: Path) -> list[Migration]:
    out: list[Migration] = []
    for file in sorted(migrations_dir.glob("*.py")):
        if not NAME_RE.match(file.stem):
            raise MigrationError(f"migration file {file.name} must be named NNNN_slug.py")
        spec = importlib.util.spec_from_file_location(f"neops_migration_{file.stem}", file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "apply", None)):
            raise MigrationError(f"migration {file.stem} has no apply(ctx)")
        out.append(Migration(file.stem, getattr(module, "DESCRIPTION", file.stem),
                             bool(getattr(module, "IDEMPOTENT", False)), module))
    return out


def pending(migrations: list[Migration], state: State) -> list[Migration]:
    done = set(state.applied_names)
    return [m for m in migrations if m.name not in done]


def snapshot(paths: Paths) -> Path:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = paths.backups / f"pre-migrate-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.backups, 0o700)
    for src in (paths.env_file, paths.state_file):
        if src.exists():
            shutil.copy2(src, target / src.name)
    return target


def _failed_marker(paths: Paths, name: str) -> Path:
    return paths.state_file.parent / f"{name}.failed"


def apply_all(env: Env, paths: Paths, state: State, log: Callable[[str], None], dry_run: bool = False) -> list[str]:
    todo = pending(discover(paths.migrations), state)
    if not todo:
        return []
    if dry_run:
        for m in todo:
            log(f"would apply {m.name}: {m.description}")
        return [m.name for m in todo]
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    snap = snapshot(paths)
    log(f"snapshot of .env and state in {snap}")
    ctx = Ctx(paths.repo, paths.data, env, log)
    done: list[str] = []
    for m in todo:
        marker = _failed_marker(paths, m.name)
        if marker.exists() and not m.idempotent:
            raise MigrationError(f"{m.name} failed earlier and is not idempotent; repair by hand using the snapshot "
                                 f"in {marker.read_text().strip() or 'backups/'} then remove {marker}")
        log(f"applying {m.name}: {m.description}")
        try:
            m.apply(ctx)
        except Exception as exc:
            marker.write_text(str(snap) + "\n")
            raise MigrationError(f"{m.name} failed: {exc}") from exc
        if marker.exists():
            marker.unlink()
        state.record_applied(m.name)
        state.save(paths.state_file)
        done.append(m.name)
    return done


def fake(name: str, paths: Paths, state: State) -> None:
    names = [m.name for m in discover(paths.migrations)]
    if name not in names:
        raise MigrationError(f"{name} is not a known migration (known: {', '.join(names)})")
    if name in state.applied_names:
        raise MigrationError(f"{name} is already applied")
    state.record_applied(name)
    state.record_faked(name)
    state.save(paths.state_file)
```

- [ ] **Step 4: Write `migrations/0001_initial_layout.py`**

```python
"""Create the data tree every bind mount points at.

Idempotent: directories that exist are left alone. Elasticsearch runs as uid 1000
and cannot create its data directory itself, so it is chowned through a throwaway
container (no sudo needed).
"""

from pathlib import Path

DESCRIPTION = "create the data/ tree and the state directory"
IDEMPOTENT = True


def apply(ctx) -> None:
    from neops_compose.paths import Paths

    paths = Paths.for_repo(ctx.repo, ctx.env)
    for d in paths.data_dirs():
        ctx.mkdir(d)
    ctx.mkdir(paths.secrets, mode=0o700)
    ctx.mkdir(paths.jwt_dir, mode=0o700)
    ctx.mkdir(paths.tls_dir, mode=0o700)
    es: Path = paths.data / "elasticsearch"
    if es.stat().st_uid != 1000:
        ctx.chown_via_container(es, 1000, 0)
```

Note for the test in Step 1: it monkeypatches `chown_via_container`, so adjust the last assertion to match the guard: the chown happens only when the directory is not already owned by 1000 (on the dev box it is owned by the developer, so the call is made — the assertion holds).

- [ ] **Step 5: Write `migrations/README.md`**

```markdown
# Deployment migrations

Numbered modules (`NNNN_slug.py`) that move an *installation* forward: renamed `.env` keys,
moved data directories, one-off fixes. Applied once, in order, by `./neops migrate` (which
`./neops up` and `./neops install` run first). The record lives in `data/.neops/state.json`.

A migration exports `DESCRIPTION`, `IDEMPOTENT` (may a crashed run simply be re-run?) and
`apply(ctx)`. `ctx` offers `repo`, `data`, `env` (`get`/`set`/`rename`/`unset`, comments kept),
`log`, `mkdir`, `move`, `chown_via_container` and `compose(*args)` for the rare migration that
needs a container (say so in the docstring). Before anything is applied, `.env` and the state
file are copied to `backups/pre-migrate-<timestamp>/`.

Do not edit a migration after it has shipped; add a new one.
```

- [ ] **Step 6: Run the tests** → `uv run pytest tests/unit/test_migrate.py -q` → `5 passed`.

- [ ] **Step 7: Commit**

```bash
git add neops_compose/migrate.py migrations
git add tests/unit/test_migrate.py
git commit -qm "feat(cli): deployment migrations with snapshots, failure markers and the initial layout"
```

---

## Task 15: `token.py`

**Files:**
- Create: `neops_compose/token.py`
- Test: `tests/unit/test_token.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_token.py
import base64
import json

from neops_compose import token
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.state import State


def fake_token(key_id: int) -> str:
    payload = json.dumps({"id": key_id, "app": "workflow", "key": "abc"})
    return base64.b64encode(f"{payload}:signature".encode()).decode()


def test_key_id_is_read_from_the_signed_payload():
    assert token.key_id(fake_token(42)) == 42


class FakeCompose:
    def __init__(self, valid: bool):
        self.valid = valid
        self.calls = []

    def exec(self, service, *cmd, env=None):
        self.calls.append((service, cmd, env))
        script = cmd[-1]
        if "get_api_key_entry" in script:
            if self.valid:
                return "VALID\n"
            raise RuntimeError("invalid api key")
        if "print('PK=" in script:
            return "PK=3\n"
        if "generate_api_key" in cmd:
            return "Your API Key is ready\n" + fake_token(9) + "\n"
        if "delete()" in script:
            return ""
        raise AssertionError(cmd)

    def up(self, *services, wait=True, force_recreate=False):
        self.calls.append(("up", services, force_recreate))


def make(tmp_path):
    (tmp_path / ".env").write_text("NEOPS_ADMIN_USER=neops\n")
    env = Env(tmp_path / ".env")
    paths = Paths.for_repo(tmp_path, env)
    paths.secrets.mkdir(parents=True)
    return env, paths


def test_ensure_is_noop_when_existing_token_is_valid(tmp_path):
    env, paths = make(tmp_path)
    paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    c = FakeCompose(valid=True)
    assert token.ensure_engine_token(c, env, paths, State(), log=lambda m: None) is False
    assert not any(call[0] == "up" for call in c.calls)


def test_ensure_mints_and_recreates_engine_when_missing_or_invalid(tmp_path):
    env, paths = make(tmp_path)
    paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    c = FakeCompose(valid=False)
    state = State()
    assert token.ensure_engine_token(c, env, paths, state, log=lambda m: None) is True
    assert paths.engine_env.read_text() == f"NEOPS_CMS_TOKEN={fake_token(9)}\n"
    assert state.api_keys[-1]["id"] == 9
    assert c.calls[-1] == ("up", ("engine",), True)


def test_rotate_revokes_the_previous_key(tmp_path):
    env, paths = make(tmp_path)
    paths.engine_env.write_text(f"NEOPS_CMS_TOKEN={fake_token(5)}\n")
    c = FakeCompose(valid=True)
    state = State()
    state.record_api_key(5, "workflow")
    token.rotate_engine_token(c, env, paths, state, log=lambda m: None)
    scripts = [call[1][-1] for call in c.calls if call[0] == "cms"]
    assert any("filter(id=5).delete()" in s for s in scripts)
    assert token.key_id(token.read_engine_token(paths)) == 9
```

- [ ] **Step 2: Run to verify failure** → collection error.

- [ ] **Step 3: Write `neops_compose/token.py`**

```python
from __future__ import annotations

import base64
import json
from collections.abc import Callable

from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.secrets import write_secret
from neops_compose.state import State

API_KEY_APP = "workflow"
API_KEY_DESCRIPTION = "neops-docker-compose: workflow engine"
ENV_KEY = "NEOPS_CMS_TOKEN"
_CHECK_SCRIPT = ("import os; from neops.enterprise.auth.static_api_key.api_key import get_api_key_entry; "
                 "get_api_key_entry(os.environ['NEOPS_CHECK_TOKEN']); print('VALID')")
_PK_SCRIPT = ("import os; from django.contrib.auth import get_user_model; "
              "print('PK=%s' % get_user_model().objects.get(username=os.environ['NEOPS_USERNAME']).pk)")
_DELETE_SCRIPT = ("from neops.enterprise.auth.static_api_key.models import StaticAPIKey; "
                  "StaticAPIKey.objects.filter(id={key_id}).delete()")


def key_id(token: str) -> int:
    """The StaticAPIKey row id, embedded in the base64(json:signature) token."""
    payload = base64.b64decode(token.encode()).decode().rsplit(":", 1)[0]
    return int(json.loads(payload)["id"])


def read_engine_token(paths: Paths) -> str | None:
    if not paths.engine_env.exists():
        return None
    for line in paths.engine_env.read_text().splitlines():
        if line.startswith(ENV_KEY + "="):
            return line.split("=", 1)[1].strip() or None
    return None


def token_is_valid(compose, token: str) -> bool:
    try:
        out = compose.exec("cms", "python", "manage.py", "shell", "-c", _CHECK_SCRIPT, env={"NEOPS_CHECK_TOKEN": token})
    except Exception:
        return False
    return "VALID" in out


def mint(compose, username: str) -> str:
    out = compose.exec("cms", "python", "manage.py", "shell", "-c", _PK_SCRIPT, env={"NEOPS_USERNAME": username})
    pk = next((line[3:] for line in out.splitlines() if line.startswith("PK=")), "")
    if not pk:
        raise RuntimeError(f"could not resolve the CMS user {username!r}")
    out = compose.exec("cms", "python", "manage.py", "generate_api_key", pk, API_KEY_APP,
                       "--description", API_KEY_DESCRIPTION)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("generate_api_key printed nothing")
    return lines[-1]


def revoke(compose, key_id_: int) -> None:
    compose.exec("cms", "python", "manage.py", "shell", "-c", _DELETE_SCRIPT.format(key_id=key_id_))


def _install(compose, paths: Paths, state: State, token: str, log: Callable[[str], None]) -> None:
    write_secret(paths.engine_env, f"{ENV_KEY}={token}\n".encode())
    state.record_api_key(key_id(token), API_KEY_APP)
    state.save(paths.state_file)
    log("recreating the engine with the new CMS token")
    compose.up("engine", force_recreate=True)


def ensure_engine_token(compose, env: Env, paths: Paths, state: State, log: Callable[[str], None]) -> bool:
    """Mint only when no valid token exists. Returns True when a token was minted."""
    existing = read_engine_token(paths)
    if existing and token_is_valid(compose, existing):
        log("engine CMS token present and valid")
        return False
    log("minting a CMS API key for the engine")
    _install(compose, paths, state, mint(compose, env.get("NEOPS_ADMIN_USER", "neops")), log)
    return True


def rotate_engine_token(compose, env: Env, paths: Paths, state: State, log: Callable[[str], None]) -> None:
    old = read_engine_token(paths)
    new = mint(compose, env.get("NEOPS_ADMIN_USER", "neops"))
    _install(compose, paths, state, new, log)
    if old:
        try:
            revoke(compose, key_id(old))
            log(f"revoked API key {key_id(old)}")
        except Exception as exc:  # the new key is live; an unrevoked old one is reported, not fatal
            log(f"warning: could not revoke the previous API key: {exc}")
```

- [ ] **Step 4: Run the tests** → `uv run pytest tests/unit/test_token.py -q` → `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add neops_compose/token.py tests/unit/test_token.py
git commit -qm "feat(cli): idempotent engine API key with explicit rotation and revocation"
```

---

## Task 16: `preflight.py`

**Files:**
- Create: `neops_compose/preflight.py`
- Test: `tests/unit/test_preflight.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_preflight.py
from neops_compose import preflight


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
    from neops_compose.env import Env
    from neops_compose.scenario import Scenario
    (tmp_path / ".env").write_text("COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.traefik-shared-host.yaml:compose.tls-files.yaml\nNEOPS_HTTP_PORT=8880\n")
    env = Env(tmp_path / ".env")
    assert preflight.required_ports(env, Scenario.from_env(env)) == [("0.0.0.0", 8880), ("0.0.0.0", 443), ("0.0.0.0", 8443)]
    (tmp_path / ".env").write_text("COMPOSE_FILE=compose.yaml:compose.expose.yaml\nNEOPS_CMS_PORT=9000\n")
    env = Env(tmp_path / ".env")
    assert preflight.required_ports(env, Scenario.from_env(env)) == [("127.0.0.1", 8080), ("127.0.0.1", 9000), ("127.0.0.1", 3030), ("127.0.0.1", 3031)]


def test_report_format():
    checks = [preflight.Check("docker", True, "29.7.2"), preflight.Check("ports", False, "443 is in use")]
    text = preflight.format_report(checks)
    assert "OK   docker" in text and "FAIL ports" in text and "443 is in use" in text
    assert preflight.all_ok(checks) is False
```

- [ ] **Step 2: Run to verify failure** → collection error.

- [ ] **Step 3: Write `neops_compose/preflight.py`**

```python
from __future__ import annotations

import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

from neops_compose.compose import Compose
from neops_compose.env import Env
from neops_compose.rules import problems
from neops_compose.scenario import Scenario

MIN_COMPOSE = (2, 24)
MIN_DISK_GIB = 5
MIN_MAX_MAP_COUNT = 262144


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def version_ok(raw: str, minimum: tuple[int, int]) -> bool:
    nums = [int(x) for x in raw.lstrip("v").split("-")[0].split(".") if x.isdigit()]
    return tuple(nums[:2]) >= minimum


def port_free(address: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((address, port))
            return True
        except OSError:
            return False


def required_ports(env: Env, scenario: Scenario) -> list[tuple[str, int]]:
    if scenario.proxy == "traefik":
        ports = [("0.0.0.0", int(env.get("NEOPS_HTTP_PORT", "80")))]
        if scenario.tls:
            ports.append(("0.0.0.0", int(env.get("NEOPS_HTTPS_PORT", "443"))))
        if scenario.shared_host:
            ports.append(("0.0.0.0", int(env.get("NEOPS_MONITOR_PORT", "8443"))))
        return ports
    bind = env.get("NEOPS_BIND_ADDRESS", "127.0.0.1")
    return [(bind, int(env.get(k, d))) for k, d in (("NEOPS_WEB_PORT", "8080"), ("NEOPS_CMS_PORT", "8000"),
                                                   ("NEOPS_ENGINE_PORT", "3030"), ("NEOPS_MONITOR_PORT", "3031"))]


def _cmd(*args: str) -> tuple[int, str]:
    r = subprocess.run(args, text=True, capture_output=True)
    return r.returncode, (r.stdout or r.stderr).strip()


def run_checks(env: Env, scenario: Scenario, repo: Path, data: Path, compose: Compose,
               check_images: bool = True) -> list[Check]:
    out: list[Check] = []
    code, docker_v = _cmd("docker", "version", "--format", "{{.Server.Version}}")
    out.append(Check("docker daemon", code == 0, docker_v if code == 0 else "docker is not running or not reachable"))
    code, compose_v = _cmd("docker", "compose", "version", "--short")
    out.append(Check("docker compose", code == 0 and version_ok(compose_v, MIN_COMPOSE),
                     compose_v if code == 0 else "docker compose v2 is required"))
    if not env.exists:
        out.append(Check(".env", False, "no .env file: copy one of examples/*.env to .env"))
        return out
    for p in problems(env, scenario, repo):
        out.append(Check(".env", False, p))
    if not any(c.name == ".env" for c in out):
        out.append(Check(".env", True, f"{len(scenario.files)} compose files, scenario valid"))

    free_gib = shutil.disk_usage(data if data.exists() else repo).free / 2**30
    out.append(Check("disk", free_gib >= MIN_DISK_GIB, f"{free_gib:.1f} GiB free under {data}"))

    mmc = Path("/proc/sys/vm/max_map_count")
    if mmc.exists():
        value = int(mmc.read_text().strip())
        out.append(Check("vm.max_map_count", value >= MIN_MAX_MAP_COUNT,
                         f"{value}" if value >= MIN_MAX_MAP_COUNT else
                         f"{value} < {MIN_MAX_MAP_COUNT}; run: sudo sysctl -w vm.max_map_count={MIN_MAX_MAP_COUNT} "
                         f"and persist it in /etc/sysctl.d/99-neops.conf"))

    try:
        running = compose.running_services() if code == 0 else set()
    except Exception:
        running = set()
    if not running:
        for address, port in required_ports(env, scenario):
            out.append(Check("ports", port_free(address, port), f"{address}:{port}" + ("" if port_free(address, port) else " is in use")))
    if check_images and code == 0 and not any(c.name == ".env" and not c.ok for c in out):
        for image in compose.images():
            rc, detail = _cmd("docker", "manifest", "inspect", image)
            out.append(Check("image", rc == 0, image if rc == 0 else f"{image}: not pullable ({detail.splitlines()[-1] if detail else 'unknown'}); run docker login quay.io"))
    for service, user, db, key in (("postgres-cms", "neops", "neops", "NEOPS_CMS_DB_PASSWORD"),
                                   ("postgres-engine", "postgres", "neops-workflow", "NEOPS_ENGINE_DB_PASSWORD"),
                                   ("postgres-keycloak", "keycloak", "keycloak", "NEOPS_KEYCLOAK_DB_PASSWORD")):
        if service in running and env.is_set(key):
            try:
                compose.exec(service, "psql", "-U", user, "-d", db, "-c", "select 1", env={"PGPASSWORD": env.get(key)})
                out.append(Check("db password", True, f"{service} accepts {key}"))
            except Exception:
                out.append(Check("db password", False, f"{service} rejects {key}: the value in .env changed without "
                                                        f"./neops rotate db-password; restore it or rotate properly"))
    return out


def all_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)


def format_report(checks: list[Check]) -> str:
    return "\n".join(f"{'OK  ' if c.ok else 'FAIL'} {c.name:<16} {c.detail}".rstrip() for c in checks)
```

- [ ] **Step 4: Run the tests** → `uv run pytest tests/unit/test_preflight.py -q` → `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add neops_compose/preflight.py tests/unit/test_preflight.py
git commit -qm "feat(cli): preflight checks (host prerequisites, scenario, images, db passwords)"
```

---
## Task 17: `doctor.py`

**Files:**
- Create: `neops_compose/doctor.py`
- Test: `tests/unit/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_doctor.py
from neops_compose import doctor
from neops_compose.urls import PublicUrl


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, url, path, method="GET", body=None, headers=None):
        self.calls.append((str(url) + path, method, headers or {}))
        return self.responses.get(path, (404, ""))


def test_container_probes_treat_completed_init_as_ok():
    rows = [{"Service": "cms-init", "State": "exited", "ExitCode": 0, "Health": ""},
            {"Service": "cms", "State": "running", "Health": "healthy"},
            {"Service": "worker", "State": "running", "Health": ""},
            {"Service": "engine", "State": "running", "Health": "unhealthy"}]
    probes = doctor.container_probes(rows, one_shots={"cms-init"})
    by = {p.name: p for p in probes}
    assert by["cms-init"].ok and by["cms"].ok and by["worker"].ok and not by["engine"].ok


def test_http_probes_hosts_mode():
    urls = {k: PublicUrl.parse(v) for k, v in {
        "NEOPS_WEB_URL": "https://neops.example.com", "NEOPS_CMS_URL": "https://cms.neops.example.com",
        "NEOPS_ENGINE_URL": "https://engine.neops.example.com", "NEOPS_WORKFLOWS_URL": "https://workflows.neops.example.com"}.items()}
    http = FakeHttp({"/": (200, "<html><app-root></app-root>"), "/admin/login/": (200, "login"),
                     "/graphql": (200, '{"data":{"__typename":"Query"}}'), "/health": (200, '{"healthy":true}'),
                     "/blackboard/job": (403, "Forbidden"), "/config.js": (200, 'apiBaseUrl: "https://engine.neops.example.com"')})
    probes = doctor.http_probes(urls, http, expect_deny=True)
    assert all(p.ok for p in probes), [p for p in probes if not p.ok]
    assert ("https://engine.neops.example.com/blackboard/job", "POST", {}) in http.calls


def test_deny_probe_fails_when_worker_api_is_reachable():
    urls = {"NEOPS_WEB_URL": PublicUrl.parse("https://neops.example.com"),
            "NEOPS_CMS_URL": PublicUrl.parse("https://cms.neops.example.com"),
            "NEOPS_ENGINE_URL": PublicUrl.parse("https://engine.neops.example.com"),
            "NEOPS_WORKFLOWS_URL": PublicUrl.parse("https://workflows.neops.example.com")}
    http = FakeHttp({"/blackboard/job": (400, "validation error")})
    deny = [p for p in doctor.http_probes(urls, http, expect_deny=True) if p.name == "engine worker API denied"][0]
    assert not deny.ok and "reachable" in deny.detail


def test_login_probe_never_accepts_500():
    cms = PublicUrl.parse("https://cms.neops.example.com")
    http = FakeHttp({"/graphql": (500, "ImproperlyConfigured RATELIMIT_IP_META_KEY")})
    p = doctor.bad_login_probe(cms, http)
    assert not p.ok and "RATELIMIT" in p.detail
    http = FakeHttp({"/graphql": (200, '{"errors":[{"message":"Please enter valid credentials"}]}')})
    assert doctor.bad_login_probe(cms, http).ok
```

- [ ] **Step 2: Run to verify failure** → collection error.

- [ ] **Step 3: Write `neops_compose/doctor.py`**

```python
from __future__ import annotations

import http.client
import json
import socket
import ssl
from dataclasses import dataclass

from neops_compose.compose import Compose
from neops_compose.env import Env
from neops_compose.scenario import Scenario
from neops_compose.urls import PublicUrl

ONE_SHOTS = {"cms-init"}
LOGIN_MUTATION = "mutation($u:String!,$p:String!){login(username:$u,password:$p){accessToken}}"


@dataclass(frozen=True)
class Probe:
    name: str
    ok: bool
    detail: str = ""


class Http:
    """Minimal HTTP client that can connect to one address while sending another Host (for boxes without DNS)."""

    def __init__(self, connect: str | None = None, insecure: bool = False, timeout: float = 15.0):
        self.connect = connect
        self.timeout = timeout
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def request(self, url: PublicUrl, path: str, method: str = "GET", body: str | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, str]:
        target = self.connect or url.host
        conn: http.client.HTTPConnection
        if url.scheme == "https":
            conn = http.client.HTTPSConnection(target, url.port, timeout=self.timeout, context=self.ctx)
            conn.sock = self.ctx.wrap_socket(socket.create_connection((target, url.port), self.timeout),
                                             server_hostname=url.host)
        else:
            conn = http.client.HTTPConnection(target, url.port, timeout=self.timeout)
        hdrs = {"Host": url.host if url.is_default_port else f"{url.host}:{url.port}", "User-Agent": "neops-doctor"}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        try:
            conn.request(method, url.path + path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read().decode(errors="replace")
        finally:
            conn.close()

    def cert_days_left(self, url: PublicUrl) -> int | None:
        if url.scheme != "https":
            return None
        import datetime as dt
        with socket.create_connection((self.connect or url.host, url.port), self.timeout) as raw:
            with self.ctx.wrap_socket(raw, server_hostname=url.host) as s:
                der = s.getpeercert(binary_form=True)
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der)
        return (cert.not_valid_after_utc - dt.datetime.now(dt.UTC)).days


def container_probes(rows: list[dict], one_shots: set[str] = ONE_SHOTS) -> list[Probe]:
    out = []
    for row in rows:
        name, state, health = row.get("Service", "?"), row.get("State", ""), row.get("Health", "")
        if name in one_shots:
            ok = state == "exited" and int(row.get("ExitCode", 1)) == 0
            out.append(Probe(name, ok, f"{state} (exit {row.get('ExitCode', '?')})"))
        else:
            ok = state == "running" and health in ("", "healthy")
            out.append(Probe(name, ok, f"{state} {health}".strip()))
    return out


def http_probes(urls: dict[str, PublicUrl], http: Http, expect_deny: bool) -> list[Probe]:
    web, cms, engine, monitor = (urls[k] for k in ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL"))
    out = []

    def get(name: str, url: PublicUrl, path: str, want: int, contains: str = "", method: str = "GET", body: str | None = None) -> None:
        try:
            status, text = http.request(url, path, method=method, body=body)
        except Exception as exc:
            out.append(Probe(name, False, f"{url}{path}: {exc}"))
            return
        ok = status == want and (contains in text)
        out.append(Probe(name, ok, f"{url}{path} -> {status}" + ("" if ok else f", expected {want}" + (f" containing {contains!r}" if contains else ""))))

    get("web client", web, "/", 200, "app-root")
    get("cms admin", cms, "/admin/login/", 200)
    get("cms graphql", cms, "/graphql", 200, "__typename", method="POST", body='{"query":"{__typename}"}')
    get("engine health", engine, "/health", 200)
    get("monitor config", monitor, "/config.js", 200, str(engine))
    if "NEOPS_KEYCLOAK_URL" in urls:
        get("keycloak realm", urls["NEOPS_KEYCLOAK_URL"], "/realms/neops/.well-known/openid-configuration", 200, "authorization_endpoint")
    if "NEOPS_GRAFANA_URL" in urls:
        get("grafana", urls["NEOPS_GRAFANA_URL"], "/api/health", 200)
    try:
        status, _ = http.request(engine, "/blackboard/job", method="POST", body="{}")
        if status == 403:
            out.append(Probe("engine worker API denied", True, f"{engine}/blackboard/job -> 403"))
        else:
            out.append(Probe("engine worker API denied", expect_deny is False,
                             f"{engine}/blackboard/job -> {status}: the worker API is reachable from outside; "
                             "your reverse proxy must deny it (see examples/external-proxy.env)"))
    except Exception as exc:
        out.append(Probe("engine worker API denied", False, f"{engine}/blackboard/job: {exc}"))
    return out


def bad_login_probe(cms: PublicUrl, http: Http) -> Probe:
    body = json.dumps({"query": LOGIN_MUTATION, "variables": {"u": "neops-doctor", "p": "not-the-password"}})
    try:
        status, text = http.request(cms, "/graphql", method="POST", body=body)
    except Exception as exc:
        return Probe("cms login path", False, str(exc))
    if status >= 500:
        return Probe("cms login path", False, f"login mutation answered {status}: {text[:160]} "
                                              "(behind an external proxy this usually means RATELIMIT_IP_META_KEY is set but X-Real-IP is not)")
    return Probe("cms login path", True, f"bad credentials answered {status} (no server error)")


def login(cms: PublicUrl, http: Http, username: str, password: str) -> str | None:
    body = json.dumps({"query": LOGIN_MUTATION, "variables": {"u": username, "p": password}})
    status, text = http.request(cms, "/graphql", method="POST", body=body)
    try:
        return json.loads(text)["data"]["login"]["accessToken"]
    except Exception:
        return None


def worker_probe(engine: PublicUrl, http: Http, token: str | None) -> Probe:
    if not token:
        return Probe("worker registered", False, "could not log in as the admin user to ask the engine")
    status, text = http.request(engine, "/workers", headers={"Authorization": f"Bearer {token}"})
    if status == 403:
        return Probe("worker registered", True, "admin token accepted, no worker:read permission (assign a role to check further)")
    if status != 200:
        return Probe("worker registered", False, f"GET /workers -> {status}")
    try:
        workers = json.loads(text)
        items = workers if isinstance(workers, list) else workers.get("items", workers.get("data", []))
        online = [w for w in items if str(w.get("status", w.get("state", ""))).upper() == "ONLINE"]
        return Probe("worker registered", bool(online), f"{len(online)} online of {len(items)} workers")
    except Exception as exc:
        return Probe("worker registered", False, f"unexpected /workers payload: {exc}")


def ratelimit_probe(cms: PublicUrl, http: Http) -> Probe:
    """Six bad logins with a different forged X-Real-IP each. Core limits local login to 5/min per IP:
    if none is refused, the proxy lets clients choose their own IP (it appends instead of overwriting)."""
    body = json.dumps({"query": LOGIN_MUTATION, "variables": {"u": "neops-doctor", "p": "not-the-password"}})
    refused = False
    for i in range(6):
        status, text = http.request(cms, "/graphql", method="POST", body=body, headers={"X-Real-IP": f"203.0.113.{i + 1}"})
        if status == 429 or "Too many" in text:
            refused = True
    return Probe("X-Real-IP trusted from client", refused,
                 "rate limit applied per real address" if refused else
                 "6 forged X-Real-IP logins were all accepted: the proxy must OVERWRITE X-Real-IP")


def run(env: Env, scenario: Scenario, compose: Compose, connect: str | None, insecure: bool,
        probe_ratelimit: bool = False) -> list[Probe]:
    probes = container_probes(compose.ps())
    urls = {k: PublicUrl.parse(env.require(k)) for k in ("NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL")}
    if scenario.keycloak:
        urls["NEOPS_KEYCLOAK_URL"] = PublicUrl.parse(env.require("NEOPS_KEYCLOAK_URL"))
    if scenario.metrics and env.is_set("NEOPS_GRAFANA_URL"):
        urls["NEOPS_GRAFANA_URL"] = PublicUrl.parse(env.get("NEOPS_GRAFANA_URL"))
    http = Http(connect=connect, insecure=insecure)
    probes += http_probes(urls, http, expect_deny=scenario.proxy == "traefik")
    probes.append(bad_login_probe(urls["NEOPS_CMS_URL"], http))
    if not scenario.oidc:
        token = login(urls["NEOPS_CMS_URL"], http, env.get("NEOPS_ADMIN_USER", "neops"), env.get("NEOPS_ADMIN_PASSWORD"))
        probes.append(worker_probe(urls["NEOPS_ENGINE_URL"], http, token))
    if probe_ratelimit:
        probes.append(ratelimit_probe(urls["NEOPS_CMS_URL"], http))
    if scenario.tls:
        try:
            days = http.cert_days_left(urls["NEOPS_WEB_URL"])
            probes.append(Probe("tls certificate", days is not None and days > 14, f"{days} days until expiry"))
        except Exception as exc:
            probes.append(Probe("tls certificate", False, str(exc)))
    return probes


def format_report(probes: list[Probe]) -> str:
    return "\n".join(f"{'OK  ' if p.ok else 'FAIL'} {p.name:<28} {p.detail}".rstrip() for p in probes)


def all_ok(probes: list[Probe]) -> bool:
    return all(p.ok for p in probes)
```

- [ ] **Step 4: Run the tests** → `uv run pytest tests/unit/test_doctor.py -q` → `4 passed`.

- [ ] **Step 5: Commit**

```bash
git add neops_compose/doctor.py tests/unit/test_doctor.py
git commit -qm "feat(cli): doctor probes through the public URLs, including the worker-API deny check"
```

---

## Task 18: `backup.py` and `rotate.py`

**Files:**
- Create: `neops_compose/backup.py`, `neops_compose/rotate.py`
- Modify: `neops_compose/compose.py` (add `exec_bytes`)
- Test: `tests/unit/test_backup.py`

- [ ] **Step 1: Add `exec_bytes` to `Compose`** (`neops_compose/compose.py`, after `exec`)

```python
    def exec_bytes(self, service: str, *cmd: str, env: dict[str, str] | None = None) -> bytes:
        flags: list[str] = []
        for key, value in (env or {}).items():
            flags += ["-e", f"{key}={value}"]
        result = subprocess.run(["docker", "compose", "exec", "-T", *flags, service, *cmd], cwd=self.repo,
                                capture_output=True)
        if result.returncode != 0:
            raise ComposeError(f"docker compose exec {service} {cmd[0]} failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout
```

- [ ] **Step 2: Write the failing backup test**

```python
# tests/unit/test_backup.py
import json
import stat

from neops_compose import backup
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.state import State


class FakeCompose:
    def running_services(self):
        return {"postgres-cms", "postgres-engine", "cms"}

    def exec_bytes(self, service, *cmd, env=None):
        return f"DUMP {service} {' '.join(cmd)}".encode()

    def images(self):
        return ["quay.io/zebbra/neops-core:2.1.0-beta.5"]


def test_backup_archive_is_self_contained_and_private(tmp_path):
    (tmp_path / ".env").write_text("COMPOSE_FILE=compose.yaml:compose.expose.yaml\nNEOPS_CMS_DB_PASSWORD=pw\nNEOPS_ENGINE_DB_PASSWORD=pw2\n")
    env = Env(tmp_path / ".env")
    paths = Paths.for_repo(tmp_path, env)
    paths.secrets.mkdir(parents=True)
    (paths.secrets / "engine.env").write_text("NEOPS_CMS_TOKEN=t\n")
    (tmp_path / "certs").mkdir()
    (tmp_path / "certs" / "cert.pem").write_text("cert")
    state = State()
    state.record_applied("0001_initial_layout")
    state.save(paths.state_file)
    target = backup.create(env, Scenario.from_env(env), paths, FakeCompose(), state, log=lambda m: None)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert (target / "cms.dump").read_bytes() == b"DUMP postgres-cms pg_dump -U neops -Fc neops"
    assert (target / "engine.dump").read_bytes() == b"DUMP postgres-engine pg_dump -U postgres -Fc neops-workflow"
    assert not (target / "keycloak.dump").exists()
    assert (target / ".env").exists() and (target / "secrets" / "engine.env").exists() and (target / "certs" / "cert.pem").exists()
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["images"] == ["quay.io/zebbra/neops-core:2.1.0-beta.5"] and manifest["applied"] == ["0001_initial_layout"]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in target.rglob("*") if p.is_file())


def test_prune_keeps_newest_n(tmp_path):
    b = tmp_path / "backups"
    for name in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z", "pre-migrate-20260104T000000Z"):
        (b / name).mkdir(parents=True)
    removed = backup.prune(b, keep=2)
    assert removed == [b / "20260101T000000Z"]
    assert (b / "pre-migrate-20260104T000000Z").exists()
```

- [ ] **Step 3: Write `neops_compose/backup.py`**

```python
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from neops_compose import __version__
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.state import State

DATABASES = (  # service, user, db, archive name
    ("postgres-cms", "neops", "neops", "cms.dump"),
    ("postgres-engine", "postgres", "neops-workflow", "engine.dump"),
    ("postgres-keycloak", "keycloak", "keycloak", "keycloak.dump"),
)


def _private_copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _lock_down(root: Path) -> None:
    os.chmod(root, 0o700)
    for p in root.rglob("*"):
        os.chmod(p, 0o700 if p.is_dir() else 0o600)


def create(env: Env, scenario: Scenario, paths: Paths, compose, state: State, log: Callable[[str], None],
           target_root: Path | None = None) -> Path:
    """Logical dumps of every running Postgres plus everything needed to rebuild: .env, secrets, certs."""
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = (target_root or paths.backups) / stamp
    target.mkdir(parents=True)
    running = compose.running_services()
    dumped = []
    for service, user, db, name in DATABASES:
        if service not in running:
            continue
        log(f"pg_dump {db} from {service}")
        (target / name).write_bytes(compose.exec_bytes(service, "pg_dump", "-U", user, "-Fc", db))
        dumped.append(name)
    _private_copy(paths.env_file, target / ".env")
    _private_copy(paths.secrets, target / "secrets")
    _private_copy(paths.certs, target / "certs")
    _private_copy(paths.repo / "cust-cert", target / "cust-cert")
    manifest = {"created": stamp, "cli": __version__, "images": compose.images(), "applied": state.applied_names,
                "faked": state.faked_names, "dumps": dumped, "compose_file": list(scenario.files),
                "note": "Elasticsearch is not backed up: after a restore run manage.py elastic_index --create and --populate"}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    _lock_down(target)
    os.chmod(target.parent, 0o700)
    log(f"backup written to {target} (this archive contains every secret of the deployment)")
    return target


def prune(backups_dir: Path, keep: int) -> list[Path]:
    archives = sorted(p for p in backups_dir.iterdir() if p.is_dir() and p.name[:1].isdigit())
    removed = archives[:-keep] if keep > 0 else []
    for p in removed:
        shutil.rmtree(p)
    return removed
```

- [ ] **Step 4: Write `neops_compose/rotate.py`**

```python
from __future__ import annotations

import secrets as pysecrets
from collections.abc import Callable

from neops_compose import secrets, token
from neops_compose.compose import Compose
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.render import KEYCLOAK_CLIENT_ID, KEYCLOAK_REALM, keycloak_relative_path, render
from neops_compose.scenario import Scenario
from neops_compose.state import State
from neops_compose.urls import PublicUrl

CORE_SERVICES = ("cms-init", "cms", "cms-worker", "cms-beat")
DB = {  # which -> (service, role, database, env key, dependants to recreate)
    "cms": ("postgres-cms", "neops", "neops", "NEOPS_CMS_DB_PASSWORD", CORE_SERVICES + ("postgres-exporter-cms",)),
    "engine": ("postgres-engine", "postgres", "neops-workflow", "NEOPS_ENGINE_DB_PASSWORD", ("engine", "postgres-exporter-engine")),
    "keycloak": ("postgres-keycloak", "keycloak", "keycloak", "NEOPS_KEYCLOAK_DB_PASSWORD", ("keycloak",)),
}
WHAT = ("db-password", "admin-password", "secret-key", "jwt", "tls", "token", "keycloak-client")


def _recreate(compose: Compose, services: tuple[str, ...]) -> None:
    present = set(compose.service_names())
    wanted = [s for s in services if s in present]
    if wanted:
        compose.up(*wanted, force_recreate=True)


def db_password(which: str, env: Env, compose: Compose, log: Callable[[str], None]) -> None:
    service, role, db, key, dependants = DB[which]
    new = pysecrets.token_hex(32)
    log(f"ALTER ROLE {role} on {service}")
    compose.exec(service, "psql", "-v", "ON_ERROR_STOP=1", "-U", role, "-d", db,
                 "-c", f"ALTER ROLE {role} WITH PASSWORD '{new}'", env={"PGPASSWORD": env.require(key)})
    env.set(key, new)
    log(f"{key} updated in .env; recreating {', '.join(dependants)}")
    _recreate(compose, dependants)


def admin_password(env: Env, compose: Compose, new: str, log: Callable[[str], None]) -> None:
    user = env.get("NEOPS_ADMIN_USER", "neops")
    compose.exec("cms", "python", "manage.py", "shell", "-c",
                 "import os; from django.contrib.auth import get_user_model; "
                 "u = get_user_model().objects.get(username=os.environ['U']); u.set_password(os.environ['P']); u.save()",
                 env={"U": user, "P": new})
    env.set("NEOPS_ADMIN_PASSWORD", new)
    log(f"password of {user} changed and stored in .env")


def secret_key(env: Env, paths: Paths, compose: Compose, state: State, log: Callable[[str], None]) -> None:
    env.set("DJANGO_SECRET_KEY", pysecrets.token_hex(32))
    log("DJANGO_SECRET_KEY rotated; every session and every static API key is now invalid")
    _recreate(compose, CORE_SERVICES)
    token.rotate_engine_token(compose, env, paths, state, log)


def jwt(paths: Paths, compose: Compose, log: Callable[[str], None]) -> None:
    for name in ("private.pem", "public.pem"):
        (paths.jwt_dir / name).unlink(missing_ok=True)
    secrets.ensure_jwt(paths.jwt_dir)
    log("JWT keypair rotated; every user session ends now")
    _recreate(compose, ("cms", "cms-worker", "cms-beat", "engine"))


def public_hosts(env: Env, scenario: Scenario) -> list[str]:
    keys = ["NEOPS_WEB_URL", "NEOPS_CMS_URL", "NEOPS_ENGINE_URL", "NEOPS_WORKFLOWS_URL"]
    if scenario.keycloak:
        keys.append("NEOPS_KEYCLOAK_URL")
    if scenario.metrics and env.is_set("NEOPS_GRAFANA_URL"):
        keys.append("NEOPS_GRAFANA_URL")
    return sorted({PublicUrl.parse(env.require(k)).host for k in keys})


def tls(env: Env, scenario: Scenario, paths: Paths, compose: Compose, log: Callable[[str], None]) -> None:
    if not env.flag("NEOPS_TLS_SELF_SIGNED"):
        raise RuntimeError("only the self-signed certificate can be rotated here; replace ./certs/*.pem by hand otherwise")
    secrets.ensure_selfsigned(paths.tls_dir, public_hosts(env, scenario), rotate=True)
    log("self-signed certificate re-issued")
    _recreate(compose, ("traefik",))


def keycloak_client(env: Env, scenario: Scenario, paths: Paths, compose: Compose, log: Callable[[str], None]) -> None:
    new = secrets.ensure_keycloak_client_secret(paths.keycloak_client_env, rotate=True)
    base = f"http://localhost:8080{keycloak_relative_path(env).rstrip('/')}"
    kcadm = "/opt/keycloak/bin/kcadm.sh"
    compose.exec("keycloak", kcadm, "config", "credentials", "--server", base, "--realm", "master",
                 "--user", "admin", "--password", env.require("NEOPS_KEYCLOAK_ADMIN_PASSWORD"))
    out = compose.exec("keycloak", kcadm, "get", "clients", "-r", KEYCLOAK_REALM, "-q", f"clientId={KEYCLOAK_CLIENT_ID}", "--fields", "id")
    import json
    client_id = json.loads(out)[0]["id"]
    compose.exec("keycloak", kcadm, "update", f"clients/{client_id}", "-r", KEYCLOAK_REALM, "-s", f"secret={new}")
    render(env, scenario, paths)
    compose.exec("cms", "python", "manage.py", "seed_oidc_providers", "--config", "/etc/neops/providers.json", "--force")
    log("Keycloak client secret rotated in Keycloak, generated/providers.json and the CMS")
```

- [ ] **Step 5: Run the tests** → `uv run pytest tests/unit/test_backup.py -q` → `2 passed`.

- [ ] **Step 6: Commit**

```bash
git add neops_compose/backup.py neops_compose/rotate.py neops_compose/compose.py tests/unit/test_backup.py
git commit -qm "feat(cli): self-contained backups and the sanctioned secret rotations"
```

---

## Task 19: `workflow.py` (install, up, keys, purge, status)

**Files:**
- Create: `neops_compose/workflow.py`
- Test: `tests/unit/test_workflow.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_workflow.py
import pytest

from neops_compose import workflow


def test_tag_of():
    assert workflow.tag_of("quay.io/zebbra/neops-core:2.1.0-beta.5") == "2.1.0-beta.5"
    assert workflow.tag_of("neops-core") == ""


def test_downgrade_detection():
    assert workflow.is_downgrade("2.1.0-beta.5", "2.1.0-beta.4") is True
    assert workflow.is_downgrade("2.1.0-beta.5", "2.1.0") is False
    assert workflow.is_downgrade("2.1.0", "2.0.9") is True
    assert workflow.is_downgrade("2.1.0", "cutting-edge") is False   # unparsable: never block
    assert workflow.is_downgrade("2.1.0", "2.1.0") is False


def test_guard_downgrade_raises_only_for_core(tmp_path):
    last = {"images": {"cms": "quay.io/zebbra/neops-core:2.1.0", "engine": "quay.io/zebbra/neops-workflow-engine:0.43.0"}}
    now = {"cms": "quay.io/zebbra/neops-core:2.0.9", "engine": "quay.io/zebbra/neops-workflow-engine:0.42.0"}
    with pytest.raises(workflow.Downgrade, match="neops-core"):
        workflow.guard_downgrade(last, now, allow=False)
    workflow.guard_downgrade(last, now, allow=True)
    workflow.guard_downgrade(None, now, allow=False)
```

- [ ] **Step 2: Run to verify failure** → collection error.

- [ ] **Step 3: Write `neops_compose/workflow.py`**

```python
from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from neops_compose import migrate, preflight, secrets, token
from neops_compose.compose import Compose
from neops_compose.doctor import all_ok as doctor_ok
from neops_compose.doctor import format_report as doctor_report
from neops_compose.doctor import run as run_doctor
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.render import render
from neops_compose.rotate import public_hosts
from neops_compose.scenario import Scenario
from neops_compose.state import State

CMS_FIRST = ("postgres-cms", "redis", "elasticsearch", "cms-init", "cms")


class Downgrade(RuntimeError):
    pass


class Blocked(RuntimeError):
    pass


@dataclass
class Ctx:
    repo: Path
    env: Env
    paths: Paths
    scenario: Scenario
    compose: Compose
    state: State
    log: Callable[[str], None]

    @classmethod
    def build(cls, repo: Path, log: Callable[[str], None]) -> Ctx:
        env = Env(repo / ".env")
        paths = Paths.for_repo(repo, env)
        return cls(repo, env, paths, Scenario.from_env(env), Compose(repo), State.load(paths.state_file), log)

    def save_state(self) -> None:
        self.state.save(self.paths.state_file)


def tag_of(image: str) -> str:
    return image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""


def _semver(tag: str) -> tuple | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z]+)\.(\d+))?$", tag)
    if not m:
        return None
    major, minor, patch, pre, pre_n = m.groups()
    return (int(major), int(minor), int(patch), 0 if pre is None else -1, int(pre_n or 0))


def is_downgrade(previous: str, current: str) -> bool:
    a, b = _semver(previous), _semver(current)
    if a is None or b is None:
        return False
    return b < a


def guard_downgrade(last_up: dict | None, images: dict[str, str], allow: bool) -> None:
    if not last_up or allow:
        return
    prev = last_up.get("images", {}).get("cms", "")
    now = images.get("cms", "")
    if "neops-core" in now and is_downgrade(tag_of(prev), tag_of(now)):
        raise Downgrade(f"neops-core would move from {tag_of(prev)} back to {tag_of(now)}; Django migrations are not "
                        "reversible. Restore a backup instead, or pass --allow-downgrade if you know better.")


def images_by_service(compose: Compose) -> dict[str, str]:
    out = {}
    for row in compose.ps():
        if row.get("Image"):
            out[row["Service"]] = row["Image"]
    return out


def check(ctx: Ctx, check_images: bool = True) -> None:
    checks = preflight.run_checks(ctx.env, ctx.scenario, ctx.repo, ctx.paths.data, ctx.compose, check_images)
    ctx.log(preflight.format_report(checks))
    if not preflight.all_ok(checks):
        raise Blocked("preflight failed; fix the FAIL lines above")
    if ctx.state.written_by_newer_cli():
        raise Blocked(f"data/ was last written by CLI {ctx.state.cli}, newer than this checkout; git pull first")


def keys(ctx: Ctx) -> None:
    if secrets.ensure_jwt(ctx.paths.jwt_dir):
        ctx.log("generated the JWT keypair")
    if ctx.scenario.keycloak:
        secrets.ensure_keycloak_client_secret(ctx.paths.keycloak_client_env)
    if ctx.env.flag("NEOPS_TLS_SELF_SIGNED"):
        hosts = public_hosts(ctx.env, ctx.scenario)
        cert = ctx.paths.tls_dir / "cert.pem"
        if cert.exists():
            missing = secrets.stale_sans(cert, hosts)
            if missing:
                raise Blocked(f"the self-signed certificate lacks {', '.join(sorted(missing))}; run ./neops rotate tls")
        elif secrets.ensure_selfsigned(ctx.paths.tls_dir, hosts):
            ctx.log(f"generated a self-signed certificate for {', '.join(hosts)}")


def migrate_all(ctx: Ctx, dry_run: bool = False) -> list[str]:
    return migrate.apply_all(ctx.env, ctx.paths, ctx.state, ctx.log, dry_run=dry_run)


def doctor(ctx: Ctx, connect: str | None = None, insecure: bool = False, probe_ratelimit: bool = False) -> bool:
    probes = run_doctor(ctx.env, ctx.scenario, ctx.compose, connect, insecure, probe_ratelimit)
    ctx.log(doctor_report(probes))
    return doctor_ok(probes)


def _finish(ctx: Ctx, connect: str | None, insecure: bool) -> None:
    ctx.state.record_up(images_by_service(ctx.compose))
    ctx.save_state()
    if not doctor(ctx, connect, insecure):
        raise Blocked("the stack is up but doctor reports failures")


def install(ctx: Ctx, connect: str | None = None, insecure: bool = False) -> None:
    check(ctx)
    migrate_all(ctx)
    keys(ctx)
    render(ctx.env, ctx.scenario, ctx.paths)
    ctx.log("pulling images")
    ctx.compose.pull()
    ctx.log("starting the CMS")
    ctx.compose.up(*CMS_FIRST)
    token.ensure_engine_token(ctx.compose, ctx.env, ctx.paths, ctx.state, ctx.log)
    ctx.log("starting everything")
    ctx.compose.up()
    _finish(ctx, connect, insecure)


def up(ctx: Ctx, allow_downgrade: bool = False, connect: str | None = None, insecure: bool = False) -> None:
    check(ctx, check_images=False)
    migrate_all(ctx)
    keys(ctx)
    render(ctx.env, ctx.scenario, ctx.paths)
    ctx.compose.pull()
    core_image = next((i for i in ctx.compose.images() if "neops-core" in i), "")
    guard_downgrade(ctx.state.last_up, {"cms": core_image}, allow_downgrade)
    ctx.compose.up()
    if token.ensure_engine_token(ctx.compose, ctx.env, ctx.paths, ctx.state, ctx.log):
        ctx.compose.up()
    _finish(ctx, connect, insecure)


def purge(ctx: Ctx, confirmed: str) -> None:
    expected = str(ctx.paths.data)
    if confirmed != expected:
        raise Blocked(f"purge removes every container and {expected}; re-run with --confirm {expected}")
    ctx.compose.down()
    for p in (ctx.paths.data, ctx.paths.generated):
        if p.exists():
            shutil.rmtree(p)
    ctx.log(f"removed {ctx.paths.data} and {ctx.paths.generated}; .env, certs/ and backups/ were kept")


def status(ctx: Ctx) -> str:
    lines = [f"scenario: {' : '.join(ctx.scenario.files)}", f"data: {ctx.paths.data}"]
    running = images_by_service(ctx.compose)
    pinned = ctx.compose.images()
    lines.append("images (pinned):")
    lines += [f"  {img}" for img in pinned]
    lines.append("images (running):")
    lines += [f"  {svc}: {img}" for svc, img in sorted(running.items())] or ["  none"]
    pend = migrate.pending(migrate.discover(ctx.paths.migrations), ctx.state)
    lines.append(f"migrations: {len(ctx.state.applied)} applied, {len(pend)} pending" +
                 (f", {len(ctx.state.faked)} FAKED ({', '.join(ctx.state.faked_names)})" if ctx.state.faked else ""))
    if ctx.state.last_up:
        lines.append(f"last successful up: {ctx.state.last_up['at']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests** → `uv run pytest tests/unit/test_workflow.py -q` → `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add neops_compose/workflow.py tests/unit/test_workflow.py
git commit -qm "feat(cli): install/up orchestration with the two-phase start and the downgrade guard"
```

---

## Task 20: `cli.py`

**Files:**
- Create: `neops_compose/cli.py`
- Test: `tests/unit/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cli.py
import subprocess
import sys

from neops_compose.cli import build_parser


def test_every_command_is_wired():
    parser = build_parser()
    subs = parser._subparsers._group_actions[0].choices
    assert set(subs) >= {"install", "up", "down", "ps", "logs", "restart", "compose", "check", "migrate", "keys",
                         "token", "render", "doctor", "status", "backup", "rotate", "purge", "version"}


def test_module_runs():
    out = subprocess.run([sys.executable, "-m", "neops_compose.cli", "version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "2.0.0"
```

- [ ] **Step 2: Write `neops_compose/cli.py`**

```python
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from neops_compose import __version__, backup, migrate, rotate, token, workflow
from neops_compose.env import MissingEnv
from neops_compose.render import render

REPO = Path(__file__).resolve().parent.parent


def log(message: str) -> None:
    print(message, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="neops", description="Operate the Neops 2.0 docker-compose deployment.")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_)

    for name, help_ in (("install", "first-time installation from a filled .env"),
                        ("up", "apply .env and repo changes: migrate, render, pull, start, doctor")):
        sp = add(name, help_)
        sp.add_argument("--connect", help="connect to this address instead of resolving the public hostnames (doctor)")
        sp.add_argument("--insecure", action="store_true", help="skip TLS verification in doctor (self-signed)")
        if name == "up":
            sp.add_argument("--allow-downgrade", action="store_true")
    add("down", "stop the stack (data is kept)")
    add("ps", "container status")
    sp = add("logs", "follow logs")
    sp.add_argument("services", nargs="*")
    sp = add("restart", "restart services")
    sp.add_argument("services", nargs="+")
    sp = add("compose", "run docker compose with this deployment's COMPOSE_FILE (prints secrets with `config`)")
    sp.add_argument("args", nargs=argparse.REMAINDER)
    sp = add("check", "preflight: host, .env, scenario, images")
    sp.add_argument("--no-images", action="store_true")
    sp = add("migrate", "apply pending deployment migrations")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--fake", metavar="NAME", help="mark a migration applied without running it (full name)")
    add("keys", "generate missing key material (never overwrites)")
    add("token", "mint the engine's CMS API key when missing or invalid")
    sp = add("render", "regenerate generated/ from .env")
    sp.add_argument("--diff", action="store_true", help="show what would change")
    sp = add("doctor", "health report through the public URLs")
    sp.add_argument("--connect")
    sp.add_argument("--insecure", action="store_true")
    sp.add_argument("--probe-ratelimit", action="store_true", help="also verify the proxy overwrites X-Real-IP (uses the login rate limit)")
    add("status", "pinned vs running images, migrations, scenario")
    sp = add("backup", "logical backup into backups/<timestamp>/")
    sp.add_argument("--dir", type=Path)
    sp.add_argument("--keep", type=int, help="prune to the newest N archives")
    sp = add("rotate", "rotate a secret: " + ", ".join(rotate.WHAT))
    sp.add_argument("what", choices=rotate.WHAT)
    sp.add_argument("--which", choices=("cms", "engine", "keycloak"), help="for db-password")
    sp.add_argument("--password", help="for admin-password (prompted when omitted)")
    sp = add("purge", "stop everything and delete the data directory")
    sp.add_argument("--confirm", default="", metavar="DATA_DIR")
    add("version", "print the CLI version")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    ctx = workflow.Ctx.build(REPO, log)
    try:
        return dispatch(args, ctx)
    except (workflow.Blocked, workflow.Downgrade, MissingEnv, migrate.MigrationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def dispatch(args: argparse.Namespace, ctx: workflow.Ctx) -> int:
    c = ctx.compose
    match args.command:
        case "install":
            workflow.install(ctx, args.connect, args.insecure)
        case "up":
            workflow.up(ctx, args.allow_downgrade, args.connect, args.insecure)
        case "down":
            c.down()
        case "ps":
            c.run("ps", "-a")
        case "logs":
            c.run("logs", "-f", "--tail", "200", *args.services, check=False)
        case "restart":
            c.run("restart", *args.services)
        case "compose":
            extra = args.args[1:] if args.args[:1] == ["--"] else args.args
            return c.run(*extra, check=False).returncode
        case "check":
            workflow.check(ctx, check_images=not args.no_images)
        case "migrate":
            if args.fake:
                print(f"faking {args.fake}: it will be recorded as applied WITHOUT running. Type the name again to confirm: ", end="")
                if input().strip() != args.fake:
                    return 1
                migrate.fake(args.fake, ctx.paths, ctx.state)
            else:
                done = workflow.migrate_all(ctx, dry_run=args.dry_run)
                log("nothing to apply" if not done else ("would apply: " if args.dry_run else "applied: ") + ", ".join(done))
        case "keys":
            workflow.keys(ctx)
        case "token":
            token.ensure_engine_token(c, ctx.env, ctx.paths, ctx.state, log)
        case "render":
            if args.diff:
                _render_diff(ctx)
            else:
                for path in render(ctx.env, ctx.scenario, ctx.paths):
                    log(f"wrote {path.relative_to(ctx.repo)}")
        case "doctor":
            return 0 if workflow.doctor(ctx, args.connect, args.insecure, args.probe_ratelimit) else 1
        case "status":
            print(workflow.status(ctx))
        case "backup":
            target = backup.create(ctx.env, ctx.scenario, ctx.paths, c, ctx.state, log, args.dir)
            if args.keep:
                for removed in backup.prune(target.parent, args.keep):
                    log(f"pruned {removed}")
        case "rotate":
            _rotate(args, ctx)
        case "purge":
            workflow.purge(ctx, args.confirm)
    return 0


def _render_diff(ctx: workflow.Ctx) -> None:
    import difflib
    import shutil
    import tempfile

    from neops_compose.paths import Paths

    before = {p: p.read_text() for p in ctx.paths.generated.rglob("*") if p.is_file()} if ctx.paths.generated.exists() else {}
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "repo"
        shutil.copytree(ctx.repo, scratch, ignore=shutil.ignore_patterns("data", "backups", ".venv", ".git", "generated"))
        alt = Paths(repo=scratch, data=ctx.paths.data)
        render(ctx.env, ctx.scenario, alt)
        after = {ctx.paths.generated / p.relative_to(alt.generated): p.read_text() for p in alt.generated.rglob("*") if p.is_file()}
    for path in sorted(set(before) | set(after)):
        a, b = before.get(path, "").splitlines(), after.get(path, "").splitlines()
        for line in difflib.unified_diff(a, b, f"generated/{path.name} (current)", f"generated/{path.name} (rendered)", lineterm=""):
            print(line)


def _rotate(args: argparse.Namespace, ctx: workflow.Ctx) -> None:
    c = ctx.compose
    match args.what:
        case "db-password":
            if not args.which:
                raise workflow.Blocked("rotate db-password needs --which cms|engine|keycloak")
            rotate.db_password(args.which, ctx.env, c, log)
        case "admin-password":
            new = args.password or getpass.getpass("new admin password: ")
            rotate.admin_password(ctx.env, c, new, log)
        case "secret-key":
            rotate.secret_key(ctx.env, ctx.paths, c, ctx.state, log)
        case "jwt":
            rotate.jwt(ctx.paths, c, log)
        case "tls":
            rotate.tls(ctx.env, ctx.scenario, ctx.paths, c, log)
        case "token":
            token.rotate_engine_token(c, ctx.env, ctx.paths, ctx.state, log)
        case "keycloak-client":
            rotate.keycloak_client(ctx.env, ctx.scenario, ctx.paths, c, log)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run the tests and the whole suite, lint**

```bash
uv run pytest -q                 # all green
uv run ruff check . && uv run ruff format --check .
./neops version                  # 2.0.0
./neops check --no-images        # on a checkout without .env: "FAIL .env  no .env file..." and exit 1
```

- [ ] **Step 4: Commit**

```bash
git add neops_compose/cli.py tests/unit/test_cli.py
git commit -qm "feat(cli): the ./neops command surface"
```

---
## Task 21: `Makefile`, compose-config gate, CI

**Files:**
- Create: `Makefile`, `tests/compose_config_check.py`, `.github/workflows/ci.yml`

- [ ] **Step 1: Write `tests/compose_config_check.py`**

```python
#!/usr/bin/env python3
"""For every examples/*.env: fill dummy secrets, render generated/, run `docker compose config`.

A cheap gate that catches a broken overlay merge or a missing interpolation before any container starts.
Run from the repo root: uv run python tests/compose_config_check.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SECRET_KEYS = ("NEOPS_CMS_DB_PASSWORD", "NEOPS_ENGINE_DB_PASSWORD", "DJANGO_SECRET_KEY", "NEOPS_ADMIN_PASSWORD",
               "NEOPS_KEYCLOAK_ADMIN_PASSWORD", "NEOPS_KEYCLOAK_DB_PASSWORD", "NEOPS_GRAFANA_ADMIN_PASSWORD",
               "NEOPS_OIDC_CLIENT_ID", "NEOPS_OIDC_CLIENT_SECRET")


def main() -> int:
    sys.path.insert(0, str(REPO))
    from neops_compose import secrets
    from neops_compose.env import Env
    from neops_compose.paths import Paths
    from neops_compose.render import render
    from neops_compose.scenario import Scenario

    failures = 0
    for example in sorted((REPO / "examples").glob("*.env")):
        with tempfile.TemporaryDirectory() as tmp:
            scratch = Path(tmp) / "repo"
            scratch.mkdir()
            for f in list(REPO.glob("compose*.yaml")) + [REPO / "metrics"]:
                (scratch / f.name).symlink_to(f)
            (scratch / "certs").mkdir()
            (scratch / "cust-cert").mkdir()
            for name in ("cert.pem", "key.pem"):
                (scratch / "certs" / name).write_text("placeholder")
            text = example.read_text()
            for key in SECRET_KEYS:
                text = text.replace(f"\n{key}=\n", f"\n{key}=dummy0123456789abcdef\n")
            (scratch / ".env").write_text(text)
            env = Env(scratch / ".env")
            paths = Paths.for_repo(scratch, env)
            for d in paths.data_dirs():
                d.mkdir(parents=True, exist_ok=True)
            secrets.ensure_jwt(paths.jwt_dir)
            secrets.ensure_keycloak_client_secret(paths.keycloak_client_env)
            if env.flag("NEOPS_TLS_SELF_SIGNED"):
                secrets.ensure_selfsigned(paths.tls_dir, ["neops.example.com"])
            paths.engine_env.write_text("NEOPS_CMS_TOKEN=dummy\n")
            render(env, Scenario.from_env(env), paths)
            result = subprocess.run(["docker", "compose", "config", "--quiet"], cwd=scratch, capture_output=True, text=True)
            status = "ok  " if result.returncode == 0 else "FAIL"
            print(f"{status} {example.name}")
            if result.returncode != 0:
                failures += 1
                print(result.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write `Makefile`**

```make
.PHONY: help check lint format test compose-config e2e

help:
	@echo "check          lint + unit tests + compose config gate for every example"
	@echo "lint           ruff check + format check"
	@echo "format         ruff format"
	@echo "test           unit tests"
	@echo "compose-config docker compose config for every examples/*.env"
	@echo "e2e            end-to-end run of one scenario: make e2e SCENARIO=traefik-tls-selfsigned"
	@echo "Operate a deployment with ./neops (see README.md)."

check: lint test compose-config

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

test:
	uv run pytest -q

compose-config:
	uv run python tests/compose_config_check.py

e2e:
	@test -n "$(SCENARIO)" || { echo "usage: make e2e SCENARIO=<examples name without .env>"; exit 1; }
	uv run python tests/e2e/run_scenario.py $(SCENARIO)
```

- [ ] **Step 3: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main, develop, "release/**"]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  check:
    name: Lint, tests, compose config
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v7
        with:
          python-version: "3.12"
          enable-cache: true
      - run: uv sync --frozen
      - run: make check
```

- [ ] **Step 4: Run and commit**

```bash
make check          # lint clean, tests green, "ok   <example>" x9
git add Makefile tests/compose_config_check.py .github/workflows/ci.yml
git commit -qm "chore: make check (lint, tests, compose-config gate) and CI"
```

---

## Task 22: `README.md` and operator docs

**Files:**
- Create: `README.md`, `docs/index.md`, `docs/10-install.md`, `docs/20-scenarios.md`, `docs/30-operations.md`, `docs/40-external-proxy.md`, `docs/50-troubleshooting.md`, `mkdocs_custom.yml`
- Vendor: `.make_scripts/mkdocs-documentation/`, `.make_scripts/release-management/` (bootstrap commands below)

- [ ] **Step 1: Write `README.md`**

```markdown
# neops-docker-compose

Production deployment of the Neops 2.0 stack with Docker Compose: neops-core (CMS), the workflow
engine, the workflow manager UI, a worker, the web client, Postgres per service, Redis and
Elasticsearch, with optional Traefik, Keycloak and a metrics stack.

Full documentation: `docs/` (and docs.neops.io once published).

## Prerequisites

- A Linux host with Docker Engine and Compose v2 (`docker compose version` ≥ 2.24), `git`, and
  [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- `docker login quay.io` with an account that can pull the licensed Neops images.
- DNS records for the public hostnames you choose (or one record in shared-hostname mode),
  and either a certificate for them or a public host for Let's Encrypt.
- `vm.max_map_count ≥ 262144` for Elasticsearch (`./neops check` tells you the exact command).

## Install

```bash
git clone https://github.com/zebbra/neops-docker-compose.git && cd neops-docker-compose
git checkout release/2.0
cp examples/traefik-tls-files.env .env      # pick the scenario that matches your setup
$EDITOR .env                                 # hostnames + every blank secret (openssl rand -hex 32)
cp /path/to/cert.pem certs/cert.pem && cp /path/to/key.pem certs/key.pem   # tls-files only
./neops install
```

`install` validates `.env`, prepares `data/`, generates keys, renders the proxy configuration,
starts the CMS, mints the engine's API key, starts everything else and runs `./neops doctor`.
Log in at `NEOPS_WEB_URL` as `NEOPS_ADMIN_USER`.

## Scenarios

| Example | What you get |
|---|---|
| `examples/external-proxy.env` | services on `127.0.0.1` ports for your own reverse proxy (see `docs/40-external-proxy.md`) |
| `examples/traefik-http.env` | bundled Traefik, plain HTTP (evaluation) |
| `examples/traefik-tls-files.env` | bundled Traefik, your certificate in `certs/` |
| `examples/traefik-tls-selfsigned.env` | bundled Traefik, a self-signed certificate minted by `./neops keys` |
| `examples/traefik-acme.env` | bundled Traefik, Let's Encrypt |
| `examples/traefik-shared-host-tls-files.env` | everything on one hostname |
| `examples/oidc-external.env` | login through your identity provider |
| `examples/oidc-keycloak.env` | login through a bundled Keycloak |
| `examples/metrics.env` | + VictoriaMetrics, Grafana, exporters |

Overlays combine: edit `COMPOSE_FILE` in `.env`; `./neops check` tells you if a combination is invalid.

## Day 2

| Command | Purpose |
|---|---|
| `./neops up` | after `git pull` or an `.env` edit: migrate, render, pull, start, doctor |
| `./neops doctor` | health through the public URLs |
| `./neops status` | pinned vs running images, migrations |
| `./neops backup --keep 14` | logical dumps + `.env` + secrets + certs into `backups/<timestamp>/` |
| `./neops rotate <what>` | `db-password --which cms|engine|keycloak`, `admin-password`, `secret-key`, `jwt`, `tls`, `token`, `keycloak-client` |
| `./neops logs cms engine` | follow logs |
| `./neops down` | stop (data kept) |
| `./neops purge --confirm <data dir>` | delete the installation |

**Backup rule.** Everything the deployment needs is `.env` plus `data/` (plus `certs/` and
`cust-cert/`). A file copy of `data/` is only valid with the stack stopped; while it runs, use
`./neops backup`. Backups contain every secret: store them accordingly. `docker system prune -a --volumes`
loses nothing.

**Upgrades.** `git pull` (or check out the next release tag), then `./neops up`. Downgrading the CMS is
refused (`Django migrations are not reversible`); restore a backup instead.

**Secrets.** Never edit a password in `.env` by hand once installed; use `./neops rotate`. `check` notices a
mismatch and names it.

**Local changes.** Put them in `compose.override.yaml` (gitignored) and append it to `COMPOSE_FILE`;
edited tracked files break the next `git pull`.
```

- [ ] **Step 2: Write the docs pages** (each a focused page; content from the spec's matching section, written for an operator):
  - `docs/index.md`: what the stack is, the service table with images and ports, the data directory tree.
  - `docs/10-install.md`: prerequisites, the install walk-through, what `install` does step by step, first login, adding users.
  - `docs/20-scenarios.md`: the overlay model, `COMPOSE_FILE` rules (`check` messages explained), the two routing modes with the URL examples from the spec, TLS modes, OIDC external vs Keycloak (redirect URI, role mapper, `resource_access`), metrics.
  - `docs/30-operations.md`: every command, backup and restore step by step (`pg_restore -U neops -d neops --clean cms.dump` inside `postgres-cms`, same for the engine and Keycloak, then `manage.py elastic_index --create` and `--populate --models core.Device core.Interface`), rotation matrix (what each rotation invalidates), migrations, upgrades and the downgrade rule, `purge`.
  - `docs/40-external-proxy.md`: the proxy contract with nginx and Caddy snippets (route by host; `proxy_set_header X-Real-IP $remote_addr; proxy_set_header X-Forwarded-Proto $scheme; client_max_body_size 200m;`; the engine deny locations for `/blackboard/job`, `/workers/register`, `/workers/*/ping`, `/workers/*/unregister`, `/function-blocks/register` as `return 403`), and the `RATELIMIT_IP_META_KEY` warning.
  - `docs/50-troubleshooting.md`: the doctor probes and what each failure means; `DisallowedHost`, 500 on login (`RATELIMIT_IP_META_KEY`), engine refusing to start (`NEOPS_CMS_TOKEN`, JWT key), Elasticsearch `max_map_count`, monitor blank (same origin), Keycloak `Invalid parameter: redirect_uri`.
  - `mkdocs_custom.yml`: `site_name: Neops docker-compose`, `nav:` listing the six pages.

- [ ] **Step 3: Vendor the shared tooling** (same mechanism as the sibling repos)

```bash
gh release download --repo zebbra/mkdocs-documentation --pattern setup_documentation.sh --output - | /bin/sh
# release-management: run the exact `sync-release-assets` recipe found in
# ../neops-lab/.make_scripts/release-management/release-management-makefile (a `gh release download … | /bin/sh` line)
```
Then add to the top of `Makefile`:
```make
include .make_scripts/mkdocs-documentation/mkdocs-documentation-makefile.mk
include .make_scripts/release-management/release-management-makefile
```
and verify `make doc-build` and `make help` work. If either bootstrap needs a `docs/` layout change, follow what the script prints; do not edit anything under `.make_scripts/`.

- [ ] **Step 4: Commit**

```bash
make doc-build
git add README.md docs mkdocs_custom.yml Makefile .make_scripts .gitignore
git commit -qm "docs: README, operator docs, vendored mkdocs and release tooling"
```

---

## Task 23: Fix the engine's monitor image publish job (upstream, cross-repo)

**Files (in `../neops-workflow-engine`, a separate git repo):**
- Modify: `.github/workflows/release-on-tag.yml` (the `publish-monitor-image` job)

Context: the job passes the npm token as `build_secrets:` but the shared action `zebbra/actions/docker-build` names the input `secrets:` (every other job in the file uses `secrets:`). The unknown input is ignored, the `client-gen` stage's `npm ci` gets no token and fails with `401`, so `quay.io/zebbra/neops-monitor-app` has never been published. The engine checkout is on `develop` with an unrelated dirty `.dockerignore`; work in a worktree so nothing of that is touched.

- [ ] **Step 1: Branch in a worktree**

```bash
cd ../neops-workflow-engine
git fetch origin develop
git worktree add /tmp/neops-engine-fix -b fix/monitor-publish-npm-secret origin/develop
cd /tmp/neops-engine-fix
```

- [ ] **Step 2: Apply the fix**

In `.github/workflows/release-on-tag.yml`, in the `publish-monitor-image` job's "Build and push Docker image" step, replace

```yaml
          build_secrets: |
            npm_token=${{ secrets.NPM_TOKEN }}
```
with
```yaml
          secrets: |
            npm_token=${{ secrets.NPM_TOKEN }}
```

Verify: `grep -n "build_secrets" .github/workflows/release-on-tag.yml` prints nothing; `grep -c "secrets: |" .github/workflows/release-on-tag.yml` is one higher than before.

- [ ] **Step 3: Commit, push, open the PR**

```bash
git commit -am "fix(ci): pass the npm token to the monitor image build

The publish-monitor-image job used build_secrets:, which the docker-build
action does not know; the client-gen stage's npm ci therefore ran without a
token and failed with 401 on every tag, so quay.io/zebbra/neops-monitor-app
was never published. The input is called secrets:, as in the other jobs."
git push -u origin fix/monitor-publish-npm-secret
gh pr create --repo zebbra/neops-workflow-engine --base develop --label pr-bugfix \
  --title "fix(ci): pass the npm token to the monitor image build" \
  --body "The \`publish-monitor-image\` job passed the token as \`build_secrets:\`, an input the \`docker-build\` action does not have, so the monitor Dockerfile's \`client-gen\` stage ran \`npm ci\` without credentials and failed with 401 on every tag (last run: 2026-09-10, tag v0.43.0-beta.2). \`quay.io/zebbra/neops-monitor-app\` has therefore never been published. Renamed to \`secrets:\`, as every other job in the file uses.

After merge, the next tag publishes the image; \`neops-docker-compose\` \`release/2.0\` pins the monitor to the engine tag and needs it."
cd ../neops-workflow-engine && git worktree remove /tmp/neops-engine-fix
```

Note the publish quirks recorded for this org: push one branch per `git push`, and the PR-label gate runs in a repo-global concurrency group, so wait for its check before opening another PR in the same repo. Tagging the engine afterwards is the user's call (`make tag-minor-beta` / `tag-latest-beta` in the engine repo, then `git push --tags`).

---

## Task 24: Local monitor image for the end-to-end runs

Until the engine tag from Task 23 exists, tests use a locally built monitor image.

- [ ] **Step 1: Build it** (from the workspace root; the Dockerfile needs the engine repo as context; bridge DNS is broken on this box, hence `--network=host`)

```bash
cd ../neops-workflow-engine
docker build --network=host --secret id=npm_token,env=NPM_TOKEN -f rest/monitor-app/Dockerfile -t neops-monitor-app:local .
docker run --rm -e ENGINE_BASE_URL=https://engine.example.com -e WEBCLIENT_ORIGIN=https://neops.example.com neops-monitor-app:local sh -c 'sleep 2; cat /usr/share/nginx/html/config.js' &
sleep 4; echo   # expected: window.__NEOPS_CONFIG__ = { apiBaseUrl: "https://engine.example.com", webclientOrigin: "https://neops.example.com" }
```

The e2e harness sets `NEOPS_MONITOR_IMAGE=neops-monitor-app:local`; `pull_policy` is not set on the service, and `docker compose pull` of a local-only image prints a warning and continues (`--ignore-pull-failures` is added by the harness through `./neops compose -- pull --ignore-pull-failures` before `install`, so `install`'s own pull finds it cached).

---

## Task 25: End-to-end harness and the per-scenario runs

**Files:**
- Create: `tests/e2e/run_scenario.py`, `tests/e2e/README.md`

Hostnames under `.localhost` resolve to `127.0.0.1` in browsers and in the harness (`--connect 127.0.0.1`), so no DNS or `/etc/hosts` change is needed. Ports: `NEOPS_HTTP_PORT=8880`, `NEOPS_HTTPS_PORT=8443`, `NEOPS_MONITOR_PORT=8444` (80/443 are taken on this box). TLS scenarios use the self-signed mode so no certificate is needed.

- [ ] **Step 1: Write `tests/e2e/run_scenario.py`**

```python
#!/usr/bin/env python3
"""End-to-end run of one examples/*.env scenario in a throwaway clone.

    uv run python tests/e2e/run_scenario.py traefik-tls-selfsigned [--keep] [--workdir DIR]

Steps: clone this checkout → write .env from the example with test hostnames, ports and secrets →
./neops install --connect 127.0.0.1 --insecure → assertions (doctor is already green: login works,
the worker is registered, install is idempotent, backup works) → ./neops down (unless --keep).
Exit code 0 only when every assertion held.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from neops_compose.doctor import Http, login  # noqa: E402
from neops_compose.urls import PublicUrl  # noqa: E402

HOST_MAP = {
    "neops.example.com": "neops.localhost",
    "cms.neops.example.com": "cms.neops.localhost",
    "engine.neops.example.com": "engine.neops.localhost",
    "workflows.neops.example.com": "workflows.neops.localhost",
    "auth.neops.example.com": "auth.neops.localhost",
    "grafana.neops.example.com": "grafana.neops.localhost",
}
PORTS = {"NEOPS_HTTP_PORT": "8880", "NEOPS_HTTPS_PORT": "8443", "NEOPS_MONITOR_PORT": "8444"}
SECRET_KEYS = ("NEOPS_CMS_DB_PASSWORD", "NEOPS_ENGINE_DB_PASSWORD", "DJANGO_SECRET_KEY", "NEOPS_ADMIN_PASSWORD",
               "NEOPS_KEYCLOAK_ADMIN_PASSWORD", "NEOPS_KEYCLOAK_DB_PASSWORD", "NEOPS_GRAFANA_ADMIN_PASSWORD")


def sh(cmd: list[str], cwd: Path, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=capture)


def test_env(example: Path, scenario: str) -> str:
    text = example.read_text()
    for old, new in HOST_MAP.items():
        text = text.replace(old, new)
    text = text.replace("https://", "https://").replace("http://", "http://")
    lines = []
    for line in text.splitlines():
        key = line.split("=", 1)[0]
        if key in SECRET_KEYS and line.endswith("="):
            line = f"{key}={secrets.token_hex(24)}"
        if key in PORTS:
            line = f"{key}={PORTS[key]}"
        lines.append(line)
    text = "\n".join(lines) + "\n"
    # non-default ports must appear in the public URLs
    if "compose.tls-" in text or "tls-selfsigned" in scenario:
        text = text.replace(".localhost\n", ".localhost:8443\n").replace(".localhost/", ".localhost:8443/")
    elif "traefik" in scenario:
        text = text.replace(".localhost\n", ".localhost:8880\n").replace(".localhost/", ".localhost:8880/")
    text = text.replace("neops.localhost:8443\nNEOPS_ENGINE_URL", "neops.localhost:8443\nNEOPS_ENGINE_URL")  # no-op guard
    text = text.replace("NEOPS_WORKFLOWS_URL=https://neops.localhost:8443", "NEOPS_WORKFLOWS_URL=https://neops.localhost:8444")
    if "tls-files" in scenario and "SELF_SIGNED" not in text:
        text += "NEOPS_TLS_SELF_SIGNED=true\nNEOPS_TLS_CERT_FILE=./data/secrets/tls/cert.pem\nNEOPS_TLS_KEY_FILE=./data/secrets/tls/key.pem\n"
        text = text.replace("NEOPS_TLS_CERT_FILE=./certs/cert.pem\nNEOPS_TLS_KEY_FILE=./certs/key.pem\n", "")
    for key in PORTS:
        if f"{key}=" not in text:
            text += f"{key}={PORTS[key]}\n"
    text += "NEOPS_MONITOR_IMAGE=neops-monitor-app:local\n"
    return text


def env_values(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and not line.startswith("#"))


def assert_(cond: bool, message: str) -> None:
    print(("PASS " if cond else "FAIL ") + message, flush=True)
    if not cond:
        raise SystemExit(f"assertion failed: {message}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--keep", action="store_true", help="leave the stack running")
    ap.add_argument("--workdir", type=Path)
    args = ap.parse_args()
    example = REPO / "examples" / f"{args.scenario}.env"
    if not example.exists():
        raise SystemExit(f"no such example: {example}")
    if args.scenario == "traefik-acme":
        raise SystemExit("traefik-acme cannot run on a laptop (needs public DNS and port 80); config-checked only")

    work = args.workdir or Path(tempfile.mkdtemp(prefix=f"neops-e2e-{args.scenario}-"))
    clone = work / "repo"
    if not clone.exists():
        sh(["git", "clone", "-q", str(REPO), str(clone)], cwd=work)
        sh(["git", "checkout", "-q", "release/2.0"], cwd=clone)
    (clone / ".env").write_text(test_env(example, args.scenario))
    values = env_values(clone / ".env")
    print(f"workdir {work}")

    sh(["./neops", "check", "--no-images"], cwd=clone)
    sh(["./neops", "compose", "--", "pull", "--ignore-pull-failures", "--quiet"], cwd=clone, check=False)
    sh(["./neops", "install", "--connect", "127.0.0.1", "--insecure"], cwd=clone)

    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    http = Http(connect="127.0.0.1", insecure=True)
    if "compose.oidc.yaml" not in values["COMPOSE_FILE"]:
        token = login(cms, http, values.get("NEOPS_ADMIN_USER", "neops"), values["NEOPS_ADMIN_PASSWORD"])
        assert_(bool(token), "admin login returns an access token")
    else:
        status, text = http.request(cms, "/graphql", method="POST", body='{"query":"{appSettings{oidcProviders{id name loginUrl}}}"}')
        assert_(status == 200 and '"oidcProviders"' in text and "keycloak" in text.lower(), f"OIDC providers are seeded ({text[:120]})")

    state_before = json.loads((clone / "data" / ".neops" / "state.json").read_text())
    sh(["./neops", "install", "--connect", "127.0.0.1", "--insecure"], cwd=clone)
    state_after = json.loads((clone / "data" / ".neops" / "state.json").read_text())
    assert_(len(state_after["api_keys"]) == len(state_before["api_keys"]) == 1, "second install mints no second API key")

    out = sh(["./neops", "backup"], cwd=clone, capture=True).stdout
    archives = list((clone / "backups").glob("2*"))
    assert_(len(archives) == 1 and (archives[0] / "cms.dump").stat().st_size > 1000, "backup produced a CMS dump")
    assert_((archives[0] / "engine.dump").exists(), "backup produced an engine dump")

    ps = sh(["./neops", "compose", "--", "ps", "--format", "json"], cwd=clone, capture=True).stdout
    assert_('"worker"' in ps and '"running"' in ps, "worker container is running")
    logs = sh(["./neops", "compose", "--", "logs", "--no-log-prefix", "worker"], cwd=clone, capture=True).stdout
    assert_("registered" in logs.lower() or "function block" in logs.lower(), "worker log shows registration")

    if not args.keep:
        sh(["./neops", "down"], cwd=clone)
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"stack left running in {clone}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Write `tests/e2e/README.md`** describing the runs, the ports, the `.localhost` trick, the local monitor image, and the browser check for Keycloak (below).

- [ ] **Step 3: Run the scenarios, one at a time, and fix what they find**

Order (each is a separate `make e2e SCENARIO=...`; budget 5–10 min each, first pull is slow):

1. `external-proxy` — the smallest stack; proves the base file, `cms-init`, the two-phase token, doctor's HTTP probes against `127.0.0.1` ports. `doctor` connects to the URLs' hosts through `--connect`; in expose mode the harness's URL hosts point at Traefik-less ports, so `NEOPS_*_URL` in this scenario must use the published ports: set them to `http://neops.localhost:8080`, `http://cms.neops.localhost:8000`, `http://engine.neops.localhost:3030`, `http://workflows.neops.localhost:3031` (extend `test_env` with an `external-proxy` special case that rewrites the four URLs this way). The deny probe is expected to report the worker API reachable here, as documented; the harness treats that probe as informational for this scenario (assert it is the only failing probe).
2. `traefik-tls-selfsigned` — Traefik, TLS, redirect, the deny rule, cert expiry probe.
3. `traefik-shared-host-tls-files` (self-signed via the harness) — core prefixes on one host, engine strip, the `:8444` monitor origin. Add to the harness for this scenario: `GET https://neops.localhost:8443/admin/login/` is 200 and its HTML references `/djstatic/`; `GET https://neops.localhost:8443/djstatic/admin/css/base.css` is 200; `GET https://neops.localhost:8443/engine/health` is 200.
4. `oidc-keycloak` — Keycloak import, provider seeding, then the browser flow with Playwright (Chromium, `ignoreHTTPSErrors`): create a user through Keycloak's admin REST API (`POST /admin/realms/neops/users` with a password credential, using an admin token from `/realms/master/protocol/openid-connect/token`), open `https://neops.localhost:8443/`, click the Keycloak login button, sign in, assert the app lands on a page that is not `/login` and that `localStorage` holds an access token. Record the steps as `tests/e2e/keycloak_login.py` using the `playwright` package (`uv run --with playwright python tests/e2e/keycloak_login.py`, after `uv run --with playwright playwright install chromium`).
5. `metrics` (self-signed via the harness) — every exporter container healthy, Grafana `/api/health` 200 via `NEOPS_GRAFANA_URL`.
6. `traefik-http` and `oidc-external` are covered by the compose-config gate; `oidc-external` cannot be exercised without an external IdP (document it).

Each run that fails is a bug in this repo until proven otherwise: fix, add a unit test where the failure was a logic error, commit, re-run. Expected first-run findings worth pre-empting: the `web` healthcheck needs `curl` in the web-client image (if absent, switch to `wget` or a Python one-liner and note it); `cms-init` timing on a cold Elasticsearch; the exact JSON shape of `GET /workers` for the worker probe.

- [ ] **Step 4: Commit the harness and any fixes**

```bash
git add tests/e2e
git commit -qm "test(e2e): scenario harness with login, idempotency, backup and worker assertions"
```

---

## Task 26: Chaos runs

**Files:**
- Create: `tests/e2e/chaos.py`

- [ ] **Step 1: Write `tests/e2e/chaos.py`** (runs against a stack left with `run_scenario.py --keep`, path passed as argument)

```python
#!/usr/bin/env python3
"""Chaos checks against a running e2e stack: uv run python tests/e2e/chaos.py <clone dir>

1. Kill each service; `docker compose` restarts it; doctor is green again.
2. Create a device in the CMS, stop the stack, `docker system prune -a --volumes` (asks for
   confirmation unless CHAOS_PRUNE=yes), `./neops up`, the device is still there and the
   engine still authenticates with the token minted before.
3. Corrupt .env in three ways; `./neops check` names each problem and exits 1.
4. Restart the engine while the worker polls; the worker re-registers.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from neops_compose.doctor import Http, login  # noqa: E402
from neops_compose.urls import PublicUrl  # noqa: E402

DEVICE_MUTATION = 'mutation{devicesUpsert(input:[{name:"chaos-device-1",platform:"linux"}]){devices{id name}}}'
DEVICE_QUERY = '{devices(filter:{name:"chaos-device-1"}){items{id name}}}'


def sh(cmd: list[str], cwd: Path, check: bool = True, capture: bool = False, env: dict | None = None):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=capture, env={**os.environ, **(env or {})})


def assert_(cond: bool, message: str) -> None:
    print(("PASS " if cond else "FAIL ") + message, flush=True)
    if not cond:
        raise SystemExit(f"assertion failed: {message}")


def doctor_ok(clone: Path) -> bool:
    return sh(["./neops", "doctor", "--connect", "127.0.0.1", "--insecure"], cwd=clone, check=False).returncode == 0


def main() -> int:
    clone = Path(sys.argv[1]).resolve()
    values = dict(line.split("=", 1) for line in (clone / ".env").read_text().splitlines() if "=" in line and not line.startswith("#"))
    cms = PublicUrl.parse(values["NEOPS_CMS_URL"])
    http = Http(connect="127.0.0.1", insecure=True)
    token = login(cms, http, values.get("NEOPS_ADMIN_USER", "neops"), values["NEOPS_ADMIN_PASSWORD"])
    auth = {"Authorization": f"Bearer {token}"}

    # 1. kill and recover
    for service in ("cms", "engine", "redis", "postgres-cms", "worker"):
        sh(["./neops", "compose", "--", "kill", service], cwd=clone)
        time.sleep(5)
        sh(["./neops", "compose", "--", "up", "-d", "--wait", "--wait-timeout", "600"], cwd=clone)
        assert_(doctor_ok(clone), f"doctor green after killing {service}")

    # 2. prune survival
    status, text = http.request(cms, "/graphql", method="POST", body=json.dumps({"query": DEVICE_MUTATION}), headers=auth)
    assert_(status == 200 and "chaos-device-1" in text, f"device created ({text[:120]})")
    token_before = (clone / "data" / "secrets" / "engine.env").read_text()
    sh(["./neops", "down"], cwd=clone)
    if os.environ.get("CHAOS_PRUNE") != "yes":
        print("about to run: docker system prune -a --volumes -f (removes ALL unused images/containers/volumes on this host)")
        if input("type PRUNE to continue: ").strip() != "PRUNE":
            return 1
    sh(["docker", "system", "prune", "-a", "--volumes", "-f"], cwd=clone)
    sh(["./neops", "compose", "--", "pull", "--ignore-pull-failures", "--quiet"], cwd=clone, check=False)
    sh(["./neops", "up", "--connect", "127.0.0.1", "--insecure"], cwd=clone)
    token = login(cms, http, values.get("NEOPS_ADMIN_USER", "neops"), values["NEOPS_ADMIN_PASSWORD"])
    status, text = http.request(cms, "/graphql", method="POST", body=json.dumps({"query": DEVICE_QUERY}), headers={"Authorization": f"Bearer {token}"})
    assert_("chaos-device-1" in text, "device survived docker system prune")
    assert_((clone / "data" / "secrets" / "engine.env").read_text() == token_before, "engine token unchanged after prune")

    # 3. corrupt .env
    original = (clone / ".env").read_text()
    for broken, needle in (
        (original.replace("NEOPS_ADMIN_PASSWORD=", "NEOPS_ADMIN_PASSWORD=changeme#"), "placeholder"),
        (original.replace("NEOPS_CMS_URL=https://", "NEOPS_CMS_URL=ftp://"), "scheme"),
        (original.replace("COMPOSE_FILE=compose.yaml:", "COMPOSE_FILE=compose.yaml:compose.expose.yaml:"), "not both"),
    ):
        (clone / ".env").write_text(broken)
        r = sh(["./neops", "check", "--no-images"], cwd=clone, check=False, capture=True)
        assert_(r.returncode == 1 and needle in (r.stdout + r.stderr), f"check blocks a broken .env ({needle})")
    (clone / ".env").write_text(original)

    # 4. engine restart under a polling worker
    sh(["./neops", "restart", "engine"], cwd=clone)
    time.sleep(45)
    assert_(doctor_ok(clone), "worker re-registered after an engine restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note: the exact GraphQL mutation for creating a device (`devicesUpsert` and its input fields) must be taken from core's schema (`neops-core/neops-graphql/schema.graphql`, search `devicesUpsert`) before the run; adjust `DEVICE_MUTATION`/`DEVICE_QUERY` to the real field names and keep the assertion.

- [ ] **Step 2: Run it against the `traefik-tls-selfsigned` stack**

```bash
uv run python tests/e2e/run_scenario.py traefik-tls-selfsigned --keep --workdir /tmp/neops-e2e-chaos
CHAOS_PRUNE=yes uv run python tests/e2e/chaos.py /tmp/neops-e2e-chaos/repo
```
`docker system prune -a --volumes` removes every unused image on this box (including the KIND and lab images), so run it only when that is acceptable; otherwise run without `CHAOS_PRUNE` and answer the prompt.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/chaos.py
git commit -qm "test(e2e): chaos checks (kill, prune survival, broken .env, engine restart)"
```

---

## Task 27: Reconcile the spec, final review, hand-back

- [ ] **Step 1: Update the spec for the deviations made while implementing**

In `docs/superpowers/specs/2026-09-14-compose-release-2.0-design.md`: remove `jinja2` and `templates/` (files are built from Python data; Traefik files are JSON, which its YAML reader accepts), remove `generated/monitor.env`, note that the engine's deny rule is method- and path-exact, note that `check`'s "forged X-Real-IP" probe became the opt-in `doctor --probe-ratelimit`, and set the status line to "implemented". Commit: `docs: reconcile the spec with the implementation`.

- [ ] **Step 2: Full verification**

```bash
make check                                     # green
make e2e SCENARIO=traefik-tls-selfsigned       # green (or the run log names what was fixed)
./neops --help
git status --short                             # clean
git log --oneline release/2.0 ^origin/develop | wc -l   # the branch's commits
```

- [ ] **Step 3: Hand back** with: the riskiest parts (the engine deny rule and the shared-host route list, both two-repo couplings; the Keycloak realm import being first-start-only), what was run (which scenarios end to end, chaos with or without the prune step) and what was not (ACME, external IdP), the state of the engine PR, and the reminder that the monitor pin resolves only after the engine is tagged.
