"""The e2e harness's workflow document and blackboard shapes, checked without an engine."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import upgrade_hook  # noqa: E402
import workflows  # noqa: E402


def test_the_function_block_id_splits_into_the_blackboards_versionable_shape():
    assert workflows.fb_parts(workflows.SAVE_FACT_FB) == {
        "package": "fb.base.neops.io",
        "name": "group_save_fact",
        "majorVersion": 0,
        "minorVersion": 1,
        "patchVersion": 0,
    }


def test_a_floating_reference_is_not_a_function_block_id():
    with pytest.raises(ValueError):
        workflows.fb_parts("fb.base.neops.io/group_save_fact:0.1")


def test_the_document_runs_the_one_function_block_on_a_group():
    document = workflows.workflow_document()
    (step,) = document["steps"]
    assert step["functionBlock"] == workflows.SAVE_FACT_FB
    assert document["seedEntity"] == document["runOn"] == step["runOn"] == "group"
    assert workflows.declared_id(document) == "wf.e2e.neops.io/group_save_fact:1.0.0"


def test_the_fact_value_is_json_text_as_the_function_blocks_json_field_wants():
    value = workflows.workflow_document()["steps"][0]["parameters"]["value"]
    assert isinstance(value, str) and isinstance(json.loads(value), dict)


def test_the_held_job_is_polled_for_the_same_function_block():
    body = json.loads(upgrade_hook.poll_body("w-1"))
    assert body["functionBlockIds"] == [workflows.fb_parts(workflows.SAVE_FACT_FB)]
    assert body["workerId"] == "w-1"


def test_the_held_result_carries_a_well_formed_lease_the_job_cannot_match():
    held = upgrade_hook.HeldJob("e", "j", "w")
    body = json.loads(upgrade_hook.result_body(held))
    assert len(body["leaseToken"]) == 64 and body["status"] == "failure"
    assert upgrade_hook.refused_for_missing_lease(403, '{"code":"LEASE_MISSING"}')
    assert not upgrade_hook.refused_for_missing_lease(403, '{"code":"LEASE_MISMATCH"}')
    assert not upgrade_hook.refused_for_missing_lease(400, "LEASE_MISSING")


@pytest.mark.parametrize("status", ["completed", "failed_safe", "failed_unsafe", "running"])
def test_a_run_is_not_settled_before_the_engine_has_unlocked_the_cms(status):
    """The engine unlocks on the way to *_ack; a run counted done before that would start the
    next run on a locked group and time a run without its unlock."""
    assert status not in workflows.SETTLED
    assert not workflows.Run("e", status, 1.0).succeeded


def test_only_the_acknowledged_completion_succeeds():
    assert workflows.SETTLED == {"completed_ack", "failed_safe_ack", "failed_unsafe_ack"}
    assert workflows.Run("e", "completed_ack", 1.0).succeeded
    assert not workflows.Run("e", "failed_safe_ack", 1.0).succeeded
