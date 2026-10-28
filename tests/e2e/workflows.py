#!/usr/bin/env python3
"""Publish and run a one-step workflow on a running deployment, and wait for it to settle.

    uv run python tests/e2e/workflows.py <deployment dir> [--connect 127.0.0.1] [--insecure]

The step is the worker's group_save_fact on a test device group, so one run produces engine
and worker lines and, because the engine locks and unlocks the group in the CMS, core's lock
and unlock lines too: every log assertion of the harness reads such a run. Prints the
execution id; exit code 0 only when the run completed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from logrecords import RunLines  # noqa: E402

from cms import GROUP_UPSERT, gql  # noqa: E402
from neops_compose.compose import Compose  # noqa: E402
from neops_compose.doctor import Http  # noqa: E402
from neops_compose.env import Env  # noqa: E402
from neops_compose.token import mint_access_token  # noqa: E402
from neops_compose.urls import PublicUrl  # noqa: E402

SAVE_FACT_FB = "fb.base.neops.io/group_save_fact:0.1.0"
WORKFLOW_PACKAGE = "wf.e2e.neops.io"
WORKFLOW_NAME = "group_save_fact"
WORKFLOW_VERSION = (1, 0, 0)
STEP_LABEL = "save_fact"
TEST_GROUP = "e2e-log-run"
FACT_KEY = "e2e_log_run"
# group_save_fact's `value` is a pydantic Json field: the parameter is the JSON text, not the value.
FACT_VALUE = json.dumps({"source": "neops-docker-compose e2e"})
SUCCEEDED = frozenset({"completed", "completed_ack"})
SETTLED = SUCCEEDED | {"failed_safe", "failed_safe_ack", "failed_unsafe", "failed_unsafe_ack"}
RUN_TIMEOUT = 90.0
LINES_TIMEOUT = 30.0
PAGE_LIMIT = 500
FB_ID = re.compile(r"^(?P<package>[^/:]+)/(?P<name>[^/:]+):(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class Deployment:
    """One installed stack: its directory, its .env and how to reach its public URLs."""

    base: Path
    values: dict[str, str]
    http: Http

    @property
    def cms(self) -> PublicUrl:
        return PublicUrl.parse(self.values["NEOPS_CMS_URL"])

    @property
    def engine(self) -> PublicUrl:
        return PublicUrl.parse(self.values["NEOPS_ENGINE_URL"])

    @property
    def compose(self) -> Compose:
        return Compose(self.base)

    @property
    def admin_user(self) -> str:
        return self.values.get("NEOPS_ADMIN_USER") or "neops"


def load_deployment(base: Path, connect: str | None = None, insecure: bool = False) -> Deployment:
    env = Env(base / ".env")
    if not env.exists:
        raise SystemExit(f"{base} does not look like an installed deployment: no .env")
    return Deployment(base, dict(env.values), Http(connect=connect, insecure=insecure))


def admin_token(deployment: Deployment) -> str:
    """Issued by the CMS itself: no login, so no rate limit, and it works under OIDC too."""
    return mint_access_token(deployment.compose, deployment.admin_user)


class Engine:
    """The engine's REST API as one principal. The token is fetched on first use, so a CMS that is
    down fails the call that needs it, and one that expired mid-run is fetched again once."""

    def __init__(self, deployment: Deployment, token_source: Callable[[], str]):
        self.deployment = deployment
        self.token_source = token_source
        self.token: str | None = None

    def bearer(self) -> str:
        if self.token is None:
            self.token = self.token_source()
        return self.token

    def call(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        status, payload = self._send(method, path, body)
        if status == 401:
            self.token = self.token_source()
            status, payload = self._send(method, path, body)
        return status, payload

    def _send(self, method: str, path: str, body: object) -> tuple[int, object]:
        status, text = self.deployment.http.request(
            self.deployment.engine,
            path,
            method=method,
            body=None if body is None else json.dumps(body),
            headers={"Authorization": f"Bearer {self.bearer()}"},
        )
        return status, parse_json(text)


def parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return text


def fb_parts(identifier: str) -> dict[str, object]:
    """A function block id as the blackboard's VersionableDto spells it."""
    match = FB_ID.match(identifier)
    if match is None:
        raise ValueError(f"not a function block id: {identifier!r}")
    return {
        "package": match["package"],
        "name": match["name"],
        "majorVersion": int(match["major"]),
        "minorVersion": int(match["minor"]),
        "patchVersion": int(match["patch"]),
    }


def workflow_document() -> dict:
    major, minor, patch = WORKFLOW_VERSION
    return {
        "type": "workflow",
        "package": WORKFLOW_PACKAGE,
        "name": WORKFLOW_NAME,
        "label": WORKFLOW_NAME,
        "majorVersion": major,
        "minorVersion": minor,
        "patchVersion": patch,
        "description": "neops-docker-compose e2e: one fact written to a test group.",
        "seedEntity": "group",
        "runOn": "group",
        "steps": [
            {
                "type": "functionBlock",
                "label": STEP_LABEL,
                "functionBlock": SAVE_FACT_FB,
                "runOn": "group",
                "parameters": {"key": FACT_KEY, "value": FACT_VALUE},
            }
        ],
    }


def declared_id(document: dict) -> str:
    version = ".".join(str(document[k]) for k in ("majorVersion", "minorVersion", "patchVersion"))
    return f"{document['package']}/{document['name']}:{version}"


def publish(engine: Engine) -> str:
    """`increment` makes a rerun against an install that holds an older copy of the document
    publish the next free version instead of answering 409; an identical copy answers 200."""
    document = workflow_document()
    status, payload = engine.call(
        "POST", "/workflow-definition/publish", {"workflow": document, "increment": True}
    )
    if status not in (200, 201):
        raise WorkflowError(f"publish answered {status}: {str(payload)[:300]}")
    return (payload.get("id") if isinstance(payload, dict) else None) or declared_id(document)


def ensure_group(deployment: Deployment, token: str) -> int:
    upserted = gql(deployment.http, deployment.cms, token, GROUP_UPSERT, {"n": TEST_GROUP})
    return int(upserted["deviceGroupUpsert"]["deviceGroup"]["id"])


@dataclass(frozen=True)
class Target:
    workflow_id: str
    group_id: int


def prepare(deployment: Deployment, engine: Engine) -> Target:
    return Target(publish(engine), ensure_group(deployment, engine.bearer()))


def execute(engine: Engine, target: Target) -> str:
    body = {
        "workflow": target.workflow_id,
        "executeOnParameters": {"ids": [target.group_id]},
        "parameters": {},
    }
    status, payload = engine.call("POST", "/workflow-execution", body)
    if status != 201 or not isinstance(payload, dict):
        raise WorkflowError(f"execute answered {status}: {str(payload)[:300]}")
    return payload["uuid"]


def status_of(engine: Engine, execution_id: str) -> str:
    status, payload = engine.call("GET", f"/workflow-execution/id/{execution_id}")
    if status != 200 or not isinstance(payload, dict):
        raise WorkflowError(f"GET /workflow-execution/id/{execution_id} answered {status}")
    return payload["status"]


@dataclass(frozen=True)
class Run:
    execution_id: str
    status: str
    seconds: float

    @property
    def succeeded(self) -> bool:
        return self.status in SUCCEEDED


def wait_settled(
    engine: Engine, execution_id: str, timeout: float = RUN_TIMEOUT, interval: float = 1.0
) -> str:
    """The last status seen: a settled one, or whatever it was when the wait ran out."""
    deadline = time.monotonic() + timeout
    status = "?"
    while time.monotonic() < deadline:
        status = status_of(engine, execution_id)
        if status in SETTLED:
            return status
        time.sleep(interval)
    return status


def run_once(engine: Engine, target: Target, timeout: float = RUN_TIMEOUT, interval: float = 1.0) -> Run:
    started = time.monotonic()
    execution_id = execute(engine, target)
    status = wait_settled(engine, execution_id, timeout, interval)
    return Run(execution_id, status, time.monotonic() - started)


def engine_logs_status(deployment: Deployment) -> dict:
    """The `logs` object of GET /version: the engine's own account of its log store."""
    status, text = deployment.http.request(deployment.engine, "/version")
    payload = parse_json(text)
    if status != 200 or not isinstance(payload, dict):
        raise WorkflowError(f"GET /version answered {status}: {text[:200]}")
    return payload.get("logs") or {}


def online_worker(engine: Engine) -> tuple[int, str | None]:
    """GET /workers' status and the uuid of one ONLINE worker; 403 means no worker:read."""
    status, payload = engine.call("GET", "/workers")
    if status != 200:
        return status, None
    items = payload if isinstance(payload, list) else payload.get("items", [])
    online = [w for w in items if str(w.get("status", "")).upper() == "ONLINE"]
    return status, (online[0].get("uuid") or online[0].get("id")) if online else None


def run_logs(engine: Engine, execution_id: str, **query: str) -> tuple[int, object]:
    """One page of GET /workflow-execution/:id/logs; `query` is passed through as given."""
    return engine.call("GET", f"/workflow-execution/{execution_id}/logs?{urlencode(query)}")


def run_lines(
    engine: Engine, execution_id: str, timeout: float = LINES_TIMEOUT
) -> tuple[RunLines, list[dict]]:
    """The run's page once every producer's lines are in, or the last page read when the wait runs
    out: worker lines arrive in batches and the store makes them readable a moment later."""
    deadline = time.monotonic() + timeout
    while True:
        status, payload = run_logs(engine, execution_id, includeCore="true", limit=str(PAGE_LIMIT))
        if status != 200 or not isinstance(payload, dict):
            raise WorkflowError(
                f"GET /workflow-execution/{execution_id}/logs answered {status}: {str(payload)[:300]}"
            )
        items = payload.get("items", [])
        lines = RunLines.count(items, execution_id)
        if not lines.problems() or time.monotonic() >= deadline:
            return lines, items
        time.sleep(2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", type=Path, help="the deployment directory (the checkout holding .env)")
    ap.add_argument("--connect", help="connect to this address instead of resolving the public hostnames")
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed certificate")
    args = ap.parse_args(argv)

    deployment = load_deployment(args.base.resolve(), args.connect, args.insecure)
    engine = Engine(deployment, lambda: admin_token(deployment))
    run = run_once(engine, prepare(deployment, engine))
    print(f"{run.execution_id} {run.status} {run.seconds:.1f}s", flush=True)
    return 0 if run.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
