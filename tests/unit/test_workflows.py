"""The e2e harness's workflow document and blackboard shapes, checked without an engine."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
    assert workflows.declared_id(document) == "wf.e2e.neops.io/group_save_fact:1.1.0"


def test_the_step_takes_its_fact_from_the_runs_parameters():
    document = workflows.workflow_document()
    assert document["steps"][0]["parameters"]["value"] == "{{ parameters.fact }}"
    assert document["parameterSchema"]["required"] == ["fact"]


def started_bodies(times: int) -> list[dict]:
    bodies = []

    def call(_method, _path, body):
        bodies.append(body)
        return 201, {"uuid": "e"}

    for _ in range(times):
        workflows.execute(SimpleNamespace(call=call), workflows.Target("wf", 3))
    return bodies


def test_every_run_writes_a_fact_value_of_its_own_so_core_has_a_change_to_audit():
    first, second = (body["parameters"]["fact"] for body in started_bodies(2))
    assert isinstance(json.loads(first), dict) and first != second


def test_the_held_job_is_polled_for_the_same_function_block():
    body = json.loads(upgrade_hook.poll_body("w-1"))
    assert body["functionBlockIds"] == [workflows.fb_parts(workflows.SAVE_FACT_FB)]
    assert body["workerId"] == "w-1"


def test_the_held_result_carries_a_well_formed_lease_the_job_cannot_match():
    held = upgrade_hook.HeldJob("e", "j", "w")
    body = json.loads(upgrade_hook.result_body(held))
    assert len(body["leaseToken"]) == 64 and body["status"] == "failed"
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


def fake_cms(monkeypatch, existing):
    calls = []

    def gql(_http, _cms, _token, query, variables):
        calls.append(query)
        if query == workflows.GROUP_READ:
            return {"groups": {"results": [{"id": i, "name": variables["n"]} for i in existing]}}
        return {"deviceGroupUpsert": {"deviceGroup": {"id": "41", "name": variables["n"]}}}

    monkeypatch.setattr(workflows, "gql", gql)
    return calls


def test_ensure_group_reuses_the_group_a_previous_run_created(monkeypatch):
    calls = fake_cms(monkeypatch, existing=["7"])
    assert workflows.ensure_group(SimpleNamespace(http=None, cms=None), "t") == 7
    assert workflows.GROUP_UPSERT not in calls


def test_ensure_group_creates_the_group_when_none_exists(monkeypatch):
    calls = fake_cms(monkeypatch, existing=[])
    assert workflows.ensure_group(SimpleNamespace(http=None, cms=None), "t") == 41
    assert calls == [workflows.GROUP_READ, workflows.GROUP_UPSERT]


def publish_answering(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(call=lambda _method, _path, _body: (201, payload))


def test_a_publish_the_engine_says_no_run_can_take_fails_at_once():
    finding = {"code": "FUNCTION_BLOCK_NOT_REGISTERED", "path": "/steps/0", "message": "no such block"}
    with pytest.raises(workflows.WorkflowError, match="FUNCTION_BLOCK_NOT_REGISTERED"):
        workflows.publish_document(publish_answering({"id": "wf", "findings": [finding]}), {})


def test_a_publish_with_only_version_findings_passes():
    finding = {"code": "FIELD_ADDED", "path": "/steps", "message": "informative"}
    assert workflows.publish_document(publish_answering({"id": "wf", "findings": [finding]}), {}) == "wf"
