"""Chaos C12b: a job polled before the upgrade, reported after it without a lease.

On the old install the e2e workflow runs with the worker stopped, and the harness polls its
first job on that worker's behalf, exactly as the 0.2 worker's poll loop would, and never
reports it. After `git pull && ./neops up` the engine holds that job without a lease, which no
worker can prove: a result for it must be refused with 403 LEASE_MISSING, and the run must end
failed once the engine cleans up the worker that vanished.

The poll goes through the engine container, never the public URL: behind Traefik the worker
routes are denied at the edge.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import inside
import workflows

HELD_FILE = "held-job.json"
POLL_TIMEOUT = 60.0
FAILED_TIMEOUT = 600.0
# Any well-formed lease: the job holds none, so the engine must refuse before comparing.
ANY_LEASE = "0" * 64
LEASE_MISSING = "LEASE_MISSING"


class HookError(RuntimeError):
    pass


@dataclass(frozen=True)
class HeldJob:
    execution_id: str
    job_id: str
    worker_id: str


def poll_body(worker_id: str) -> str:
    return json.dumps(
        {"functionBlockIds": [workflows.fb_parts(workflows.SAVE_FACT_FB)], "workerId": worker_id, "limit": 1}
    )


def result_body(held: HeldJob) -> str:
    return json.dumps(
        {
            "jobExecutionId": held.job_id,
            "workerId": held.worker_id,
            "leaseToken": ANY_LEASE,
            "status": "failed",
            "result": {"success": False, "message": "neops-docker-compose e2e: reported without a lease"},
        }
    )


def poll_as(base: Path, worker_id: str) -> str | None:
    status, body = inside.from_engine(
        base, "POST", f"{inside.ENGINE_INTERNAL}/blackboard/job", poll_body(worker_id)
    )
    if status not in (200, 201):
        raise HookError(f"POST /blackboard/job answered {status}: {body[:200]}")
    jobs = json.loads(body or "[]")
    return jobs[0]["jobId"] if jobs else None


def hold_job(deployment: workflows.Deployment, engine: workflows.Engine, work: Path) -> HeldJob:
    """Stops the worker for good: the upgrade's `up` is what starts the new one."""
    status, worker_id = workflows.online_worker(engine)
    if worker_id is None:
        raise HookError(f"no online worker to hold a job for (GET /workers answered {status})")
    deployment.compose.run("stop", "worker")
    execution_id = workflows.execute(engine, workflows.prepare(deployment, engine))
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        job_id = poll_as(deployment.base, worker_id)
        if job_id:
            held = HeldJob(execution_id, job_id, worker_id)
            (work / HELD_FILE).write_text(json.dumps(asdict(held), indent=2) + "\n")
            return held
        time.sleep(2)
    raise HookError(f"run {execution_id} offered no {workflows.SAVE_FACT_FB} job within {POLL_TIMEOUT:.0f}s")


def report_without_lease(deployment: workflows.Deployment, held: HeldJob) -> tuple[int | None, str]:
    return inside.from_engine(
        deployment.base, "POST", f"{inside.ENGINE_INTERNAL}/blackboard/job/result", result_body(held)
    )


def refused_for_missing_lease(status: int | None, body: str) -> bool:
    return status == 403 and LEASE_MISSING in body


def final_status(engine: workflows.Engine, held: HeldJob) -> str:
    return workflows.wait_settled(engine, held.execution_id, timeout=FAILED_TIMEOUT, interval=5.0)
