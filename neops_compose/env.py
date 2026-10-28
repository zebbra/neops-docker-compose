from __future__ import annotations

import re
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
        set_key(str(self.path), key, value, quote_mode="auto")
        self.values[key] = value
        self.exists = True

    def unset(self, key: str) -> None:
        if key in self.values:
            unset_key(str(self.path), key)
            del self.values[key]

    def rename(self, old: str, new: str) -> None:
        """In place when `old` sits on exactly one `KEY=` line, keeping its position and comment.
        Otherwise the value compose reads (the last occurrence) moves to `new`."""
        if not self.exists or old not in self.values:
            raise MissingEnv(old)
        if new in self.values:
            raise ValueError(f"cannot rename {old} to {new}: {new} already exists")
        lines = self.path.read_text().splitlines(keepends=True)
        any_form = re.compile(rf"^[ \t]*(?:export[ \t]+)?{re.escape(old)}[ \t]*=")
        exact = re.compile(rf"^(?P<prefix>[ \t]*(?:export[ \t]+)?){re.escape(old)}=")
        occurrences = [i for i, line in enumerate(lines) if any_form.match(line)]
        match = exact.match(lines[occurrences[0]]) if len(occurrences) == 1 else None
        if match is None:
            self.set(new, self.values[old])
            self.unset(old)
            return
        i = occurrences[0]
        lines[i] = f"{match.group('prefix')}{new}={lines[i][match.end() :]}"
        self.path.write_text("".join(lines))
        self.values[new] = self.values.pop(old)
