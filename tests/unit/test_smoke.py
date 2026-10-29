"""The smoke suite's verdicts, checked on captured output without a stack."""

import sys
from pathlib import Path

import pytest

from neops_compose.doctor import LOGS_PROBE, NAME_WIDTH

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import smoke  # noqa: E402


def doctor_line(status: str, name: str, detail: str) -> str:
    return f"{status} {name:<{NAME_WIDTH}} {detail}"


def test_one_failed_check_fails_the_run_and_not_applicable_does_not():
    assert smoke.exit_code([smoke.Check("S1", smoke.PASS, ""), smoke.Check("S3", smoke.NA, "")]) == 0
    assert smoke.exit_code([smoke.Check("S1", smoke.PASS, ""), smoke.Check("S2", smoke.FAIL, "")]) == 1


def test_a_check_prints_on_one_line_with_its_verdict_first():
    assert smoke.Check("S10", smoke.PASS, "log_outbox rows: 0").line() == "PASS S10 log_outbox rows: 0"


def test_doctor_needs_its_log_store_line_ok_when_the_logs_overlay_is_on():
    ok_line = doctor_line("OK  ", LOGS_PROBE, "victorialogs ok")
    warn_line = doctor_line("WARN", LOGS_PROBE, "victorialogs is unreachable")
    assert smoke.doctor_verdict(0, ok_line, expects_logs_probe=True)[0]
    assert not smoke.doctor_verdict(0, warn_line, expects_logs_probe=True)[0]
    assert not smoke.doctor_verdict(0, "", expects_logs_probe=True)[0]
    assert smoke.doctor_verdict(0, "", expects_logs_probe=False)[0]
    assert not smoke.doctor_verdict(1, ok_line, expects_logs_probe=True)[0]


@pytest.mark.parametrize(
    ("logs", "has_store", "ok"),
    [
        ({"queryable": True, "state": "ok", "entriesDropped": 0}, True, True),
        ({"queryable": True, "state": "open", "entriesDropped": 0}, True, False),
        ({"queryable": True, "state": "ok", "entriesDropped": 3}, True, False),
        ({"queryable": False, "state": None}, False, True),
        ({"queryable": True, "state": "ok", "entriesDropped": 0}, False, False),
    ],
)
def test_version_verdict(logs, has_store, ok):
    assert smoke.version_verdict(logs, has_store)[0] is ok


@pytest.mark.parametrize(
    ("line", "error"),
    [
        ('{"level":"error","message":"boom"}', True),
        ('{"level":"info","message":"an ERROR in a message is not a level"}', False),
        ('{"levelname":"CRITICAL","msg":"x"}', True),
        ("2026-10-28 10:00:00 ERROR neops.core something failed", True),
        ("2026-10-28 10:00:00 INFO neops.core all fine", False),
        ("{not json but ERROR", True),
    ],
)
def test_each_format_is_judged_by_its_own_level(line, error):
    assert smoke.is_error_line(line) is error


def test_a_lease_less_report_after_an_upgrade_is_tolerated():
    lines = ['{"level":"error","message":"403 LEASE_MISSING for job j"}', '{"level":"error","message":"x"}']
    assert smoke.error_lines(lines) == [lines[1]]


def test_line_formats_counts_json_and_text():
    assert smoke.line_formats(['{"a":1}', "plain"]) == "1 json/1 text"


def test_every_check_id_has_a_check():
    assert all(hasattr(smoke.Smoke, c.lower()) for c in smoke.CHECK_IDS)
