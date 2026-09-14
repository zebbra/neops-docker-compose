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
