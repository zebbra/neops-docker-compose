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


def test_env_set_quotes_values_that_need_it(tmp_path):
    p = tmp_path / ".env"
    env = Env(p)
    env.set("NEOPS_OIDC_NAME", "My Company # 1")
    assert Env(p).get("NEOPS_OIDC_NAME") == "My Company # 1"


def test_env_rename_preserves_export_prefix(tmp_path):
    p = write(tmp_path / ".env", "export OLD=1\n")
    env = Env(p)
    env.rename("OLD", "NEW")
    text = p.read_text()
    assert "export NEW=1" in text
    assert "OLD" not in text


def test_env_rename_preserves_indentation(tmp_path):
    p = write(tmp_path / ".env", "  OLD=1\n")
    env = Env(p)
    env.rename("OLD", "NEW")
    text = p.read_text()
    assert "  NEW=1" in text
    assert "OLD" not in text


def test_env_rename_missing_key_raises(tmp_path):
    p = write(tmp_path / ".env", "A=1\n")
    env = Env(p)
    with pytest.raises(MissingEnv, match="OLD"):
        env.rename("OLD", "NEW")


def test_env_rename_missing_file_raises_missing_env(tmp_path):
    env = Env(tmp_path / ".env")
    with pytest.raises(MissingEnv, match="OLD"):
        env.rename("OLD", "NEW")


def test_env_rename_existing_target_raises(tmp_path):
    p = write(tmp_path / ".env", "OLD=1\nNEW=2\n")
    env = Env(p)
    with pytest.raises(ValueError, match="already exists"):
        env.rename("OLD", "NEW")


def test_paths_default_data_dir_is_under_repo(tmp_path):
    env = Env(write(tmp_path / ".env", ""))
    paths = Paths.for_repo(tmp_path, env)
    assert paths.data == tmp_path / "data"
    assert paths.secrets == tmp_path / "data" / "secrets"
    assert paths.state_file == tmp_path / "data" / ".neops" / "state.json"
    assert paths.generated == tmp_path / "generated"


def test_paths_honour_neops_data_dir_relative_to_repo(tmp_path):
    env = Env(write(tmp_path / ".env", "NEOPS_DATA_DIR=/srv/neops\n"))
    assert Paths.for_repo(tmp_path, env).data == Path("/srv/neops").resolve()
    env = Env(write(tmp_path / ".env", "NEOPS_DATA_DIR=./elsewhere\n"))
    assert Paths.for_repo(tmp_path, env).data == tmp_path / "elsewhere"
