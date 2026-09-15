import json
import stat

import pytest

from neops_compose.state import SCHEMA, State, StateError


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


def test_prerelease_and_v_prefix_order_by_pep440(tmp_path):
    p = tmp_path / "state.json"
    s = State.load(p)
    s.cli = "2.0.0-beta.1"
    s.save(p)
    assert State.load(p).cli == "2.0.0", "a beta of this version is older, so saving stamps the release"
    s.cli = "v2.0.0"
    assert s.written_by_newer_cli() is False, "v2.0.0 and 2.0.0 are the same version"
    s.cli = "2.0.1"
    assert s.written_by_newer_cli() is True


def test_unparsable_version_sorts_lowest(tmp_path):
    p = tmp_path / "state.json"
    s = State.load(p)
    s.cli = "not-a-version"
    assert s.written_by_newer_cli() is False
    s.save(p)
    assert State.load(p).cli == "2.0.0"


def test_temp_file_never_exists_group_readable(tmp_path):
    p = tmp_path / ".neops" / "state.json"
    s = State.load(p)
    s.save(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not list(p.parent.glob("*.tmp")), "the temp file is renamed into place, never left behind"


def test_corrupt_json_raises_state_error(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    with pytest.raises(StateError, match="not a valid state file"):
        State.load(p)


def test_non_list_field_raises_state_error(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"schema": SCHEMA, "applied": "0001_initial_layout"}))
    with pytest.raises(StateError, match="applied must be a list"):
        State.load(p)


def test_a_json_scalar_is_not_a_state_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("42")
    with pytest.raises(StateError, match="expected an object"):
        State.load(p)


def test_newer_schema_tells_the_operator_to_update(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"schema": SCHEMA + 1, "cli": "9.0.0"}))
    with pytest.raises(StateError, match="update the CLI"):
        State.load(p)


def test_non_object_last_up_raises_state_error(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"schema": SCHEMA, "last_up": "2026-09-14"}))
    with pytest.raises(StateError, match="last_up must be an object"):
        State.load(p)
