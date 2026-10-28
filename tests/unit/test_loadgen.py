"""loadgen.py's parsers and pacing, and the harness's accounting of its lines, without Docker or the SDK.

The SDK messages below are copied from neops-worker-sdk-py (neops_worker_sdk/logger/push/pipeline.py,
delivery.py, utils/retry.py, worker/worker_registration.py, registry/registry.py): loadgen counts
what the SDK did by them, so a reworded SDK message has to fail here, not pass silently in a run.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import chaos_kit  # noqa: E402
import loadgen  # noqa: E402
import loadgen_driver  # noqa: E402
import workflows  # noqa: E402

from .test_chaos import make_clone  # noqa: E402

WORKER = "6f1c2a4e-0d0b-4a53-9d2e-3c1e9b7a5f10"
JOB = "2b7d0c3e-1111-4c4c-8888-0a0b0c0d0e0f"
SDK = {
    "registered worker": f"Worker registered (uuid={WORKER}, version=0.3.0, name=e2e-loadgen, state=ONLINE)",
    "registered blocks": "All 1 function block(s) registered successfully",
    "dropped": "dropped 1234 log lines (buffer full)",
    "unpushed": "7 log line(s) were not pushed to the engine.",
    "refused": "The engine refused 3 log line(s) (HTTP 413); they are dropped: (413)",
    "rejected": "The engine refused 2 log line(s): seq 4 TIMESTAMP_BEFORE_JOB, seq 5 TIMESTAMP_BEFORE_JOB",
    "lease": f"The engine refuses log lines of job {JOB}; its lines are dropped.",
    "retry": "Log push failed (ServiceException: (503)\nReason: Service Unavailable\n); retrying in 2s",
}


def test_the_block_id_is_a_function_block_id_the_blackboard_takes():
    assert loadgen.BURST_FB == "fb.e2e.neops.io/log_burst:0.1.0"
    assert workflows.fb_parts(loadgen.BURST_FB)["name"] == loadgen.BURST_NAME


def test_the_burst_workflow_runs_the_loadgen_block_globally():
    document = loadgen_driver.burst_workflow_document()
    (step,) = document["steps"]
    assert step["functionBlock"] == loadgen.BURST_FB
    assert document["seedEntity"] == "global"
    assert workflows.declared_id(document) == "wf.e2e.neops.io/log_burst:1.0.0"


def test_the_sdk_messages_are_counted_into_the_summary():
    notices = loadgen.Notices()
    seen = [notices.observe(message) for message in SDK.values()]
    assert seen == [
        "worker_id",
        "registered",
        "dropped",
        "unpushed",
        "refused",
        "refused",
        "lease_refused",
        "retry",
    ]
    summary = notices.summary
    assert (summary.worker_id, summary.dropped, summary.unpushed, summary.refused) == (WORKER, 1234, 7, 5)
    assert (summary.lease_refused, summary.retry_statuses, summary.otherwise_lost) == (1, [503], 13)
    assert notices.registered


def test_drops_add_up_across_flushes_and_retries_count_each_status_once():
    notices = loadgen.Notices()
    for message in (SDK["dropped"], "dropped 6 log lines (buffer full)", SDK["retry"], SDK["retry"]):
        notices.observe(message)
    assert notices.summary.dropped == 1240 and notices.summary.retry_statuses == [503]


def test_a_burst_line_is_no_sdk_notice():
    assert loadgen.Notices().observe(loadgen.BURST_LINE.format(5)) is None


def test_a_marker_survives_the_round_trip_and_the_last_summary_wins():
    first = loadgen.Summary(worker_id=WORKER, sent=10, dropped=1, retry_statuses=[503], exit_code=0)
    last = loadgen.Summary(worker_id=WORKER, sent=20, exit_code=0)
    lines = [
        loadgen.marker_line(loadgen.READY, {"worker_id": WORKER}),
        '{"level": "warn", "message": "retrying"}',
        loadgen.marker_line(loadgen.SUMMARY, vars(first)),
        loadgen.marker_line(loadgen.SUMMARY, vars(last)),
    ]
    assert loadgen.parse_marker(lines[0]) == (loadgen.READY, {"worker_id": WORKER})
    assert loadgen.summary_of(lines) == last


@pytest.mark.parametrize("line", ["", "LOADGEN", "LOADGEN READY", "LOADGEN READY {nope", "LOADGEN READY [1]"])
def test_anything_else_is_no_marker(line):
    assert loadgen.parse_marker(line) is None


def test_a_run_that_died_has_no_summary():
    assert loadgen.summary_of([loadgen.marker_line(loadgen.READY, {"worker_id": WORKER})]) is None


def test_a_summary_from_a_newer_loadgen_keeps_the_fields_it_knows():
    assert loadgen.Summary.from_payload({"sent": 3, "future": True}).sent == 3


@pytest.mark.parametrize(
    "elapsed, due", [(-1.0, 0), (0.0, 0), (0.05, 20), (1.0, 400), (2.5, 1000), (99.0, 1000)]
)
def test_lines_are_due_on_schedule_and_never_past_the_total(elapsed, due):
    assert loadgen.lines_due(400, loadgen.burst_total(400, 2.5), elapsed) == due


class Clock:
    def __init__(self, step: float):
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_the_burst_logs_numbered_lines_at_its_rate_and_says_when_it_starts(monkeypatch, capsys):
    monkeypatch.setattr(loadgen, "TICK_SECONDS", 0)
    burst = loadgen.Burst(rate=50, seconds=2, clock=Clock(0.1))
    logged: list[str] = []
    assert asyncio.run(burst.emit(logged.append)) == 100
    assert [loadgen.burst_index(line) for line in logged] == list(range(100))
    assert burst.done.is_set() and burst.elapsed >= 2
    assert loadgen.parse_marker(capsys.readouterr().out)[0] == loadgen.BURST


@pytest.mark.parametrize(
    "argv", [[], ["--rate", "10"], ["--rate", "0", "--seconds", "5"], ["--rate", "10", "--seconds", "-1"]]
)
def test_rate_and_seconds_are_required_and_above_zero(argv):
    with pytest.raises(SystemExit):
        loadgen.parse_args(argv)


def test_the_arguments_parse_to_numbers():
    args = loadgen.parse_args(["--rate", "400", "--seconds", "90"])
    assert (args.rate, args.seconds) == (400, 90.0)


def test_loadgen_refuses_to_run_outside_the_worker_image():
    assert "URL_BLACKBOARD" in loadgen.refusal({})


def test_loadgen_refuses_a_push_level_that_sends_nothing():
    assert "off" in loadgen.refusal({"URL_BLACKBOARD": "http://engine:3030", "NEOPS_LOG_PUSH_LEVEL": "OFF"})


def test_loadgen_runs_in_a_worker_container():
    assert loadgen.refusal({"URL_BLACKBOARD": "http://engine:3030", "NEOPS_LOG_PUSH_LEVEL": "info"}) is None


def test_the_burst_block_is_the_only_one_and_the_worker_is_named():
    environ = {"DIR_FUNCTION_BLOCKS": "neops/fb", "WORKER_NAME": "neops-worker"}
    loadgen.prepare_environment(environ)
    assert environ == {"WORKER_NAME": loadgen.WORKER_NAME, "NEOPS_LOG_LEVEL": loadgen.STDOUT_LEVEL}


def test_loadgen_runs_as_a_one_off_worker_without_its_dependencies():
    clone = Path("/work/repo")
    command = loadgen_driver.loadgen_command(clone, "neops-e2e-logs-18000-loadgen-ab", 400, 90.0)
    assert command[:5] == ["./neops", "compose", "--", "run", "--rm"]
    assert "--no-deps" in command and "-T" in command
    assert f"{clone}/tests/e2e/loadgen.py:/loadgen.py:ro" in command
    worker = command.index("worker")
    assert command[worker + 1 :] == [
        loadgen_driver.WORKER_PYTHON,
        "/loadgen.py",
        "--rate",
        "400",
        "--seconds",
        "90.0",
    ]


def test_each_loadgen_container_has_a_name_of_its_project():
    first, second = (loadgen_driver.container_name("neops-e2e-logs-18000") for _ in range(2))
    assert first.startswith("neops-e2e-logs-18000-loadgen-") and first != second


def test_loadgen_is_never_started_against_anything_but_a_harness_clone(tmp_path, monkeypatch):
    monkeypatch.setattr(loadgen_driver, "LoadgenProcess", pytest.fail)
    live = make_clone(tmp_path, project="neops")
    load = loadgen_driver.BurstLoad(live, "neops", engine=None, rate=10, seconds=1)
    with pytest.raises(loadgen_driver.LoadgenError, match="COMPOSE_PROJECT_NAME"):
        load.launch()


def test_loadgen_is_refused_while_the_shell_carries_compose_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(loadgen_driver, "LoadgenProcess", pytest.fail)
    monkeypatch.setenv("COMPOSE_FILE", "compose.yaml")
    load = loadgen_driver.BurstLoad(
        make_clone(tmp_path), "neops-e2e-logs-18000", engine=None, rate=10, seconds=1
    )
    with pytest.raises(loadgen_driver.LoadgenError, match="COMPOSE_FILE"):
        load.launch()


def record(seq: int, message: str = "line", job_id: str | None = JOB) -> dict:
    stored = {"service": "neops-worker", "process": WORKER, "seq": str(seq), "message": message}
    return stored | ({"job_id": job_id} if job_id else {})


def test_received_is_sent_less_what_was_dropped_by_seq_in_each_series():
    job = [record(i, loadgen.BURST_LINE.format(i)) for i in (0, 1, 2, 6, 7, 8, 9)]
    own = [record(0, "Worker registered", None), record(2, SDK["dropped"], None)]
    lines = loadgen_driver.account(job + own)
    assert (lines.assigned, lines.received, lines.missing) == (13, 9, 4)
    assert (lines.burst_received, lines.newest_burst, lines.drop_warnings, lines.duplicates) == (7, 9, 1, 0)


def test_a_line_stored_twice_is_a_duplicate_not_a_second_line():
    lines = loadgen_driver.account([record(0), record(1), record(1)])
    assert (lines.received, lines.duplicates, lines.missing) == (2, 1, 0)


def test_nothing_stored_accounts_for_nothing():
    lines = loadgen_driver.account([])
    assert (lines.assigned, lines.received, lines.newest_burst) == (0, 0, None)


def test_a_workers_store_query_asks_for_its_process_and_its_messages():
    query = chaos_kit.store_query(
        "2026-10-28T10:00:00Z", "2026-10-28T10:05:00Z", 'process:="w"', ("seq", "_msg")
    )
    assert query == '_time:[2026-10-28T10:00:00Z, 2026-10-28T10:05:00Z] process:="w" | fields seq, _msg'


def test_a_stored_line_comes_back_under_the_engines_message_key():
    assert chaos_kit.as_log_record({"seq": "1", "_msg": "hi"}) == {"seq": "1", "message": "hi"}


def stack(load: str) -> chaos_kit.Stack:
    values = {
        "COMPOSE_PROJECT_NAME": "neops-e2e-logs-18000",
        "NEOPS_ENGINE_URL": "https://engine.neops.localhost",
    }
    return chaos_kit.Stack(Path("/work/repo"), values, None, load)


def test_the_steps_under_load_get_loadgen_by_default_for_longer_than_their_fault():
    load = chaos_kit.make_load(stack(chaos_kit.LOADGEN_LOAD), seconds=120)
    assert isinstance(load, chaos_kit.LoadgenLoad)
    assert (load.burst.rate, load.burst.seconds) == (chaos_kit.BURST_RATE, 120 + chaos_kit.LOAD_LEAD_SECONDS)


def test_the_workflow_load_stays_as_the_fallback():
    assert isinstance(
        chaos_kit.make_load(stack(chaos_kit.WORKFLOW_LOAD), seconds=120), chaos_kit.WorkflowLoad
    )


def test_a_load_stack_keeps_the_load_it_was_given(tmp_path):
    clone = make_clone(tmp_path)
    assert chaos_kit.load_stack(clone, chaos_kit.WORKFLOW_LOAD).load == chaos_kit.WORKFLOW_LOAD
    assert chaos_kit.load_stack(clone).load == chaos_kit.LOADGEN_LOAD


@pytest.mark.parametrize(
    "payload, states",
    [
        ([{"uuid": "a", "status": "online"}], {"a": "ONLINE"}),
        ({"items": [{"id": "b", "status": "OFFLINE"}]}, {"b": "OFFLINE"}),
        ("forbidden", {}),
    ],
)
def test_the_worker_list_reads_in_either_shape(payload, states):
    assert workflows.worker_states(payload) == states


def test_online_worker_picks_an_online_one():
    engine = SimpleNamespace(
        call=lambda method, path: (
            200,
            [{"uuid": "a", "status": "OFFLINE"}, {"uuid": "b", "status": "ONLINE"}],
        )
    )
    assert workflows.online_worker(engine) == (200, "b")
