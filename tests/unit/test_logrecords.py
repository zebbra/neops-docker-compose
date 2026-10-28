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


def test_loki_skips_the_victorialogs_only_checks_and_asks_loki_for_readiness():
    values = {"COMPOSE_FILE": "compose.yaml:compose.expose.yaml", "NEOPS_LOG_BACKEND": "loki"}
    assert "grafana-datasource" not in logrecords.log_checks(values)
    assert logrecords.store_of(values).ready_url == "http://loki:3100/ready"


def test_without_a_store_the_only_check_is_that_logs_are_not_queryable():
    assert logrecords.log_checks({"COMPOSE_FILE": "compose.yaml:compose.expose.yaml"}) == ("not-queryable",)


def test_a_complete_series_has_no_problems():
    assert logrecords.seq_problems([line("neops-core", s) for s in (4, 2, 3)]) == []


def test_a_hole_and_a_duplicate_are_both_reported():
    problems = logrecords.seq_problems([line("neops-core", s) for s in (1, 2, 2, 5)])
    assert problems == ["neops-core/p: duplicate seq 2", "neops-core/p: 2 missing seq 3, 4"]


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


def test_run_lines_need_every_producer_and_no_other_run():
    records = [
        {"service": "neops-workflow-engine", "workflow_execution_id": RUN},
        {"service": "neops-worker", "workflow_execution_id": RUN},
        {"service": "neops-core", "workflow_execution_id": RUN, "message": "Locked group 3"},
        {"service": "neops-core", "workflow_execution_id": RUN, "message": "Unlocked group 3"},
    ]
    assert logrecords.RunLines.count(records, RUN).problems() == []
    leaked = records[1:] + [{"service": "neops-worker", "workflow_execution_id": OTHER_RUN}]
    assert logrecords.RunLines.count(leaked, RUN).problems() == ["no engine line", "1 lines of another run"]


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


def test_a_missing_required_key_is_named():
    record = {k: v for k, v in GOOD.items() if k != "message"}
    assert logrecords.schema_errors(record, SCHEMA) == ["$: missing message"]
