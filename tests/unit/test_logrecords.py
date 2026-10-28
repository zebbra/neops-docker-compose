"""The e2e harness's judgement of log records, checked without a stack."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import logrecords  # noqa: E402

RUN = "0b5d4c43-4a0e-4c8e-9a52-6f1d8f2b8a10"
OTHER_RUN = "7f0e1b9c-2d3a-4e5f-8a6b-1c2d3e4f5a6b"
SCHEMA = {
    "type": "object",
    "required": ["service", "level", "seq", "process", "message"],
    "additionalProperties": False,
    "properties": {
        "service": {"$ref": "#/definitions/LogService"},
        "level": {"type": "string", "enum": ["debug", "info", "warn", "error"]},
        "seq": {"type": "integer", "minimum": 0},
        "process": {"type": "string", "maxLength": 8},
        "message": {"type": "string", "minLength": 1},
        "workflow_execution_id": {"type": "string", "format": "uuid"},
        "fields": {
            "type": "object",
            "maxProperties": 2,
            "additionalProperties": {"type": "string"},
            "propertyNames": {"pattern": "^[a-z][a-z0-9_]{0,63}$", "not": {"enum": ["service_name"]}},
        },
    },
    "definitions": {"LogService": {"type": "string", "enum": ["neops-core", "neops-worker"]}},
}
GOOD = {"service": "neops-core", "level": "info", "seq": 3, "process": "h:1", "message": "m"}


def line(service: str, seq: int, process: str = "p", **extra) -> dict:
    return {"service": service, "seq": seq, "process": process, **extra}


@pytest.mark.parametrize(
    ("env", "backend"),
    [
        (
            {"COMPOSE_FILE": "compose.yaml:compose.traefik.yaml:compose.grafana.yaml:compose.logs.yaml"},
            "victorialogs",
        ),
        ({"COMPOSE_FILE": "compose.yaml:compose.expose.yaml", "NEOPS_LOG_BACKEND": "loki"}, "loki"),
        ({"COMPOSE_FILE": "compose.yaml:compose.expose.yaml"}, "db"),
        ({"COMPOSE_FILE": "compose.yaml:compose.expose.yaml", "NEOPS_LOG_BACKEND": "db"}, "db"),
    ],
)
def test_the_backend_follows_the_logs_overlay_then_the_env(env, backend):
    assert logrecords.log_backend(env) == backend


def test_victorialogs_checks_include_its_grafana_datasource():
    checks = logrecords.log_checks({"COMPOSE_FILE": "compose.yaml:compose.grafana.yaml:compose.logs.yaml"})
    assert "grafana-datasource" in checks and "worker-lines" in checks


LOKI = {"COMPOSE_FILE": "compose.yaml:compose.expose.yaml", "NEOPS_LOG_BACKEND": "loki"}


def test_loki_skips_the_victorialogs_only_checks_and_asks_loki_for_readiness():
    assert "grafana-datasource" not in logrecords.log_checks(LOKI)
    assert logrecords.store_of(LOKI).ready_url == "http://loki:3100/ready"


def test_loki_checks_that_no_victorialogs_runs_and_its_ingest_keys_are_dropped():
    checks = logrecords.log_checks(LOKI)
    assert "sole-store" in checks and "no-ingest-keys" in checks
    assert logrecords.other_store_services(logrecords.store_of(LOKI)) == ["victorialogs"]


def test_victorialogs_has_no_ingest_keys_to_check():
    checks = logrecords.log_checks({"COMPOSE_FILE": "compose.yaml:compose.grafana.yaml:compose.logs.yaml"})
    assert "sole-store" in checks and "no-ingest-keys" not in checks


def test_ingest_keys_are_found_at_the_top_and_among_the_fields():
    records = [
        {"service": "neops-worker", "service_name": "x"},
        {"service": "neops-worker", "fields": {"detected_level": "info"}},
        {"service": "neops-worker", "fields": {"device_ip": "1"}},
    ]
    problems = logrecords.ingest_key_problems(records, logrecords.LOKI_INGEST_KEYS)
    assert problems == ["detected_level on 1 of 3 records", "service_name on 1 of 3 records"]
    assert logrecords.ingest_key_problems(records[2:], logrecords.LOKI_INGEST_KEYS) == []


def test_without_a_store_the_only_check_is_that_logs_are_not_queryable():
    assert logrecords.log_checks({"COMPOSE_FILE": "compose.yaml:compose.expose.yaml"}) == ("not-queryable",)


def test_a_complete_series_has_no_problems():
    assert logrecords.seq_problems([line("neops-core", s) for s in (4, 2, 3)]) == []


def test_a_hole_and_a_duplicate_are_both_reported():
    problems = logrecords.seq_problems([line("neops-core", s) for s in (1, 2, 2, 5)])
    assert problems == ["neops-core/p: duplicate seq 2", "neops-core/p: 2 missing seq 3, 4"]


def test_holes_can_be_left_out_where_a_kill_makes_them_expected():
    records = [line("neops-core", s) for s in (1, 1, 5)]
    assert logrecords.seq_problems(records, holes_in=()) == ["neops-core/p: duplicate seq 1"]


def test_holes_can_be_checked_in_some_services_only():
    """A failed store probe takes an engine seq and is never spooled, so an outage leaves
    engine holes by design; the worker's series must still be whole."""
    records = [line("neops-workflow-engine", s) for s in (1, 4)]
    records += [line("neops-worker", s, "w", job_id="j") for s in (0, 2)]
    assert logrecords.seq_problems(records, holes_in=(logrecords.WORKER_SERVICE,)) == [
        "neops-worker/w/j: 1 missing seq 1"
    ]


def test_a_restarted_engine_starts_a_new_series_rather_than_a_hole():
    records = [line("neops-workflow-engine", s, "a:1") for s in (7, 8)]
    records += [line("neops-workflow-engine", s, "a:2") for s in (0, 1)]
    assert logrecords.seq_problems(records) == []


def test_worker_seq_counts_per_job():
    records = [line("neops-worker", s, "w", job_id="j1") for s in (0, 1)]
    records += [line("neops-worker", s, "w", job_id="j2") for s in (0, 1)]
    assert logrecords.seq_problems(records) == []


def test_key_set_names_producer_fields_apart():
    records = [{"service": "x", "fields": {"device_ip": "1"}}, {"seq": 1}]
    assert logrecords.key_set(records) == ["fields.device_ip", "seq", "service"]


UNLOCK_AUDIT = "lock wf:3f2a:1: applied PATCH on group 3 -- actor: neops (id=1), agent: neops-workflow-engine"


def test_run_lines_need_every_producer_and_no_other_run():
    records = [
        {"service": "neops-workflow-engine", "workflow_execution_id": RUN},
        {"service": "neops-worker", "workflow_execution_id": RUN},
        {"service": "neops-core", "workflow_execution_id": RUN, "message": UNLOCK_AUDIT},
    ]
    assert logrecords.RunLines.count(records, RUN).problems() == []
    leaked = records[1:] + [{"service": "neops-worker", "workflow_execution_id": OTHER_RUN}]
    assert logrecords.RunLines.count(leaked, RUN).problems() == ["no engine line", "1 lines of another run"]


def test_core_s_unlock_audit_line_is_recognised_by_its_shape():
    assert logrecords.is_unlock_audit({"service": "neops-core", "message": UNLOCK_AUDIT})
    assert not logrecords.is_unlock_audit({"service": "neops-worker", "message": UNLOCK_AUDIT})


@pytest.mark.parametrize(
    "message",
    [
        "Lock refused for group 3: already held by wf:1",
        "Unlocked group 3",
        "Failed to acquire lock. Retrying in 1 seconds. Time left: 4",
    ],
)
def test_other_core_lines_mentioning_locks_are_not_the_audit_line(message):
    assert not logrecords.is_unlock_audit({"service": "neops-core", "message": message})


def test_a_run_without_core_s_audit_line_names_it():
    records = [
        {"service": "neops-workflow-engine", "workflow_execution_id": RUN},
        {"service": "neops-worker", "workflow_execution_id": RUN},
        {"service": "neops-core", "workflow_execution_id": RUN, "message": "Lock refused for group 3"},
    ]
    assert logrecords.RunLines.count(records, RUN).problems() == ["no core unlock audit line"]


def test_level_problems_name_lines_below_warn():
    records = [{"level": "warn"}, {"level": "error"}, {"level": "info", "message": "hello"}]
    assert logrecords.level_problems(records) == ["info: hello"]


def test_a_valid_record_has_no_schema_errors():
    assert logrecords.schema_errors(GOOD | {"workflow_execution_id": RUN}, SCHEMA) == []


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"service": "neops-nope"}, "is not one of"),
        ({"seq": -1}, "below 0"),
        ({"seq": True}, "expected integer"),
        ({"process": "123456789"}, "longer than 8"),
        ({"message": ""}, "shorter than 1"),
        ({"workflow_execution_id": "not-a-uuid"}, "is not a uuid"),
        ({"service_name": "x"}, "unexpected key service_name"),
        ({"fields": {"service_name": "x"}}, "reserved key"),
        ({"fields": {"Bad": "x"}}, "does not match"),
        ({"fields": {"a": 1}}, "expected string"),
        ({"fields": {"a": "1", "b": "2", "c": "3"}}, "more than 2 properties"),
    ],
)
def test_each_schema_rule_is_enforced(change, error):
    errors = logrecords.schema_errors(GOOD | change, SCHEMA)
    assert any(error in e for e in errors), errors


@pytest.mark.parametrize(
    ("value", "ok"),
    [("2026-10-28T09:15:00.123Z", True), ("2026-10-28T09:15:00+02:00", True), ("yesterday", False)],
)
def test_date_time_format_is_checked(value, ok):
    schema = {"type": "string", "format": "date-time"}
    assert (logrecords.schema_errors(value, schema) == []) == ok


def test_const_is_checked():
    schema = {"type": "string", "const": "neops-worker"}
    assert logrecords.schema_errors("neops-worker", schema) == []
    assert logrecords.schema_errors("neops-core", schema) == ["$: 'neops-core' is not 'neops-worker'"]


def test_a_missing_required_key_is_named():
    record = {k: v for k, v in GOOD.items() if k != "message"}
    assert logrecords.schema_errors(record, SCHEMA) == ["$: missing message"]
