from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion, Version

from neops_compose import __version__
from neops_compose.secrets import write_secret

SCHEMA = 1
LIST_FIELDS = ("applied", "faked", "api_keys")


class StateError(RuntimeError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _version(v: str) -> Version:
    """PEP 440 ordering, so 2.0.0b1 sorts below 2.0.0. An unreadable version sorts lowest."""
    try:
        return Version(v)
    except InvalidVersion:
        return Version("0")


def _parse(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise StateError(f"{path} is not a valid state file: {exc}") from exc
    if not isinstance(raw, dict):
        raise StateError(f"{path} is not a valid state file: expected an object, got {type(raw).__name__}")
    return raw


def _validate(path: Path, raw: dict) -> None:
    schema = raw.get("schema", SCHEMA)
    if isinstance(schema, int) and schema > SCHEMA:
        raise StateError(
            f"{path} was written with state schema {schema}, but this CLI understands {SCHEMA}: "
            "update the CLI (git pull) before running it against this installation"
        )
    for name in LIST_FIELDS:
        if name in raw and not isinstance(raw[name], list):
            raise StateError(f"{path} is not a valid state file: {name} must be a list")
    if raw.get("last_up") is not None and not isinstance(raw["last_up"], dict):
        raise StateError(f"{path} is not a valid state file: last_up must be an object")


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
        raw = _parse(path)
        _validate(path, raw)
        return cls(
            schema=raw.get("schema", SCHEMA),
            cli=raw.get("cli", "0.0.0"),
            applied=raw.get("applied", []),
            faked=raw.get("faked", []),
            api_keys=raw.get("api_keys", []),
            last_up=raw.get("last_up"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.cli = __version__ if _version(self.cli) <= _version(__version__) else self.cli
        tmp = path.with_suffix(".tmp")
        write_secret(tmp, (json.dumps(asdict(self), indent=2) + "\n").encode())
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
        return _version(self.cli) > _version(__version__)
