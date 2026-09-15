import pytest

from neops_compose import workflow


def test_tag_of():
    assert workflow.tag_of("quay.io/zebbra/neops-core:2.1.0-beta.5") == "2.1.0-beta.5"
    assert workflow.tag_of("neops-core") == ""


def test_downgrade_detection():
    assert workflow.is_downgrade("2.1.0-beta.5", "2.1.0-beta.4") is True
    assert workflow.is_downgrade("2.1.0-beta.5", "2.1.0") is False
    assert workflow.is_downgrade("2.1.0", "2.0.9") is True
    assert workflow.is_downgrade("2.1.0", "cutting-edge") is False  # unparsable: never block
    assert workflow.is_downgrade("2.1.0", "2.1.0") is False


def test_guard_downgrade_raises_only_for_core(tmp_path):
    last = {
        "images": {
            "cms": "quay.io/zebbra/neops-core:2.1.0",
            "engine": "quay.io/zebbra/neops-workflow-engine:0.43.0",
        }
    }
    now = {
        "cms": "quay.io/zebbra/neops-core:2.0.9",
        "engine": "quay.io/zebbra/neops-workflow-engine:0.42.0",
    }
    with pytest.raises(workflow.Downgrade, match="neops-core"):
        workflow.guard_downgrade(last, now, allow=False)
    workflow.guard_downgrade(last, now, allow=True)
    workflow.guard_downgrade(None, now, allow=False)
