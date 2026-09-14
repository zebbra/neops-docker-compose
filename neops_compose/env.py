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
        self.path.write_text(
            text.replace(f"\n{old}=", f"\n{new}=", 1)
            if not text.startswith(f"{old}=")
            else text.replace(f"{old}=", f"{new}=", 1)
        )
        self.values[new] = value
        del self.values[old]
