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
