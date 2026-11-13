"""Core settings an operator adds through compose.override.yaml, the one compose file that is theirs.

`environment:` beats `env_file:` in compose, so a key set here reaches core even though
generated/cms.env does not carry it. The file is round-tripped, never regenerated: services,
keys and comments the operator wrote by hand survive every edit.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Iterable
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

from neops_compose.env import Env
from neops_compose.render import CMS_ENV_KEYS
from neops_compose.rules import ALL_SECRET_KEYS
from neops_compose.scenario import BASE_FILE, OVERLAYS, OVERRIDE_FILE, Scenario

# A service runs core's code exactly when it reads core's rendered settings.
CORE_ENV_FILE = "./generated/cms.env"
KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
HEADER = (
    "Local to this host and gitignored: compose merges it over the shipped files\n"
    "when COMPOSE_FILE in .env names it.\n"
    "./neops config set/unset manage the environment of the core services here; anything\n"
    "else you add by hand, and your comments, are kept."
)


class OverrideError(Exception):
    pass


def core_services(repo: Path) -> tuple[str, ...]:
    services = _safe_load(repo / BASE_FILE).get("services", {})
    return tuple(name for name, spec in services.items() if CORE_ENV_FILE in _env_file_paths(spec))


def managed_keys(repo: Path) -> frozenset[str]:
    """Every key the shipped compose files or generated/cms.env already give a core service,
    which an override would silently shadow, and the .env secrets, which an operator could
    believe they change through it."""
    core = core_services(repo)
    keys = {*CMS_ENV_KEYS, *ALL_SECRET_KEYS}
    for name in (BASE_FILE, *OVERLAYS.values()):
        services = _safe_load(repo / name).get("services") or {}
        for service in core:
            keys |= _environment(services.get(service)).keys()
    return frozenset(keys)


def parse_assignments(items: Iterable[str]) -> dict[str, str]:
    out = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise OverrideError(f"{item!r} is not KEY=VALUE")
        out[_valid_key(key)] = value
    return out


def set_settings(repo: Path, values: dict[str, str]) -> None:
    for key in values:
        _valid_key(key)
    clash = sorted(values.keys() & managed_keys(repo))
    if clash:
        raise OverrideError(
            f"{', '.join(clash)} already set by the shipped compose files or generated/cms.env; "
            "an override would silently shadow it. Change it in .env where it is configurable "
            "(see docs/20-scenarios.md)"
        )
    doc = _load(repo)
    if not (repo / OVERRIDE_FILE).is_file():
        doc.yaml_set_start_comment(HEADER)
    services = doc.setdefault("services", CommentedMap())
    for service in core_services(repo):
        block = services.get(service)
        if block is None:
            block = services[service] = CommentedMap()
        _check_own(block, service)
        _assign(block, values)
    _save(repo, doc)


def unset_settings(repo: Path, keys: Iterable[str]) -> list[str]:
    """The keys that were set and are now gone; the file is untouched when that is none."""
    wanted = list(dict.fromkeys(_valid_key(k) for k in keys))
    doc = _load(repo)
    services = doc.get("services") or {}
    removed: set[str] = set()
    for service in core_services(repo):
        block = services.get(service)
        if not isinstance(block, dict) or block.get("environment") is None:
            continue
        _check_own(block, service)
        removed |= _remove(block, wanted)
        if not block:
            del services[service]
    if removed:
        _save(repo, doc)
    return [k for k in wanted if k in removed]


def settings(repo: Path) -> dict[str, dict[str, str | None]]:
    """What the override sets for each core service, as core sees it (`$$` read back as `$`)."""
    services = _load(repo).get("services") or {}
    out = {}
    for service in core_services(repo):
        env = _environment(services.get(service))
        if env:
            out[service] = {k: None if v is None else v.replace("$$", "$") for k, v in env.items()}
    return out


def require_dot_env(env: Env) -> None:
    if not env.exists:
        raise OverrideError(f"{env.path} does not exist: fill in .env and run ./neops install first")


def ensure_listed(env: Env) -> bool:
    """Appends the override to COMPOSE_FILE through the same writer migrations use. Appended,
    so it is merged last and wins; an operator who placed it elsewhere keeps their order."""
    require_dot_env(env)
    files = Scenario.from_env(env).files
    if OVERRIDE_FILE in files:
        return False
    env.set("COMPOSE_FILE", env.get("COMPOSE_PATH_SEPARATOR", ":").join((*files, OVERRIDE_FILE)))
    return True


def _valid_key(key: str) -> str:
    if not KEY_RE.match(key):
        raise OverrideError(f"{key!r} is not an environment variable name (A-Z, 0-9 and _)")
    return key


def _assign(block: CommentedMap, values: dict[str, str]) -> None:
    escaped = {k: v.replace("$", "$$") for k, v in values.items()}
    env = block.get("environment")
    if env is None:
        block["environment"] = CommentedMap(escaped)
        return
    if isinstance(env, dict):
        env.update(escaped)
        return
    _check_list(env)
    for key, value in escaped.items():
        entry = f"{key}={value}"
        at = [i for i, item in enumerate(env) if _list_key(item) == key]
        for i in reversed(at[1:]):
            del env[i]
        if at:
            env[at[0]] = entry
        else:
            env.append(entry)


def _remove(block: CommentedMap, keys: list[str]) -> set[str]:
    env = block["environment"]
    if isinstance(env, dict):
        found = {k for k in keys if k in env}
        for key in found:
            del env[key]
    else:
        _check_list(env)
        found = {_list_key(item) for item in env} & set(keys)
        env[:] = [item for item in env if _list_key(item) not in found]
    if not env:
        del block["environment"]
    return found


def _environment(spec: object) -> dict[str, str | None]:
    if not isinstance(spec, dict):
        return {}
    env = spec.get("environment")
    if isinstance(env, dict):
        return {str(k): None if v is None else str(v) for k, v in env.items()}
    if isinstance(env, list):
        pairs = (str(item).partition("=") for item in env)
        return {k: (v if sep else None) for k, sep, v in pairs}
    return {}


def _list_key(item: object) -> str:
    return str(item).partition("=")[0]


def _env_file_paths(spec: object) -> list[str]:
    if not isinstance(spec, dict):
        return []
    entries = spec.get("env_file") or []
    if isinstance(entries, str | dict):
        entries = [entries]
    return [e["path"] if isinstance(e, dict) else e for e in entries]


def _safe_load(path: Path) -> dict:
    return YAML(typ="safe", pure=True).load(path) or {}


def _round_trip() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


def _load(repo: Path) -> CommentedMap:
    path = repo / OVERRIDE_FILE
    if not path.is_file():
        return CommentedMap()
    try:
        doc = _round_trip().load(path)
    except YAMLError as exc:
        raise OverrideError(
            f"{OVERRIDE_FILE} is not valid YAML; fix it by hand, it was left unchanged:\n{exc}"
        ) from exc
    if doc is None:
        return CommentedMap()
    _check_mapping(doc, "the document")
    if doc.get("services") is not None:
        _check_mapping(doc["services"], "services")
    return doc


def _check_mapping(node: object, where: str) -> None:
    if not isinstance(node, dict):
        raise OverrideError(
            f"{OVERRIDE_FILE}: {where} is not a mapping; fix it by hand, it was left unchanged"
        )


def _check_own(block: object, service: str) -> None:
    """An anchored, aliased or merged-in environment is shared with whatever else uses it,
    so editing it would change services other than this one."""
    _check_mapping(block, f"services.{service}")
    env = block.get("environment")
    own = dict(block.non_merged_items())
    if _anchored(block) or ("environment" in block and "environment" not in own) or _anchored(env):
        raise OverrideError(
            f"{OVERRIDE_FILE}: the environment of {service} is shared through a YAML anchor or merge; "
            f"give {service} an environment of its own by hand, the file was left unchanged"
        )


def _anchored(node: object) -> bool:
    anchor = getattr(node, "anchor", None)
    return anchor is not None and anchor.value is not None


def _check_list(node: object) -> None:
    if not isinstance(node, list):
        raise OverrideError(
            f"{OVERRIDE_FILE}: an environment is neither a mapping nor a list; fix it by hand, "
            "it was left unchanged"
        )


def _save(repo: Path, doc: CommentedMap) -> None:
    """Whole or not at all: compose must never read a half-written override."""
    buf = io.StringIO()
    _round_trip().dump(doc, buf)
    path = (repo / OVERRIDE_FILE).resolve()
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(buf.getvalue())
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o7777)
    os.replace(tmp, path)
