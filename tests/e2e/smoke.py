#!/usr/bin/env python3
"""Post-deploy smoke suite S1-S13 for centralized logging, against one deployment directory.

    uv run python tests/e2e/smoke.py --base <deployment dir> [--connect 127.0.0.1] [--insecure]
        [--min-dashboards N] [--record-schema PATH] [--dump-keys PATH] [--only S7 ...]

Read-only except S7, which publishes wf.e2e.neops.io/group_save_fact and runs it once on the
test group e2e-log-run (both kept, so a rerun reuses them), and S9, which creates a CMS user
without any workflow grant, asks with its token and deletes it again. The engine token is
issued by the CMS for NEOPS_ADMIN_USER (`docker compose exec cms`), so no login is spent.
Prints one line per check; exit code 0 only when no check failed. N/A is not a failure: it
marks a check that does not apply to this deployment's log store.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import inside  # noqa: E402
import logrecords  # noqa: E402
import workflows  # noqa: E402
from run_scenario import (  # noqa: E402
    REPO,
    grafana_dashboards,
    grafana_datasource_types,
    neops,
    shell_compose_refusal,
)

from neops_compose.compose import Compose  # noqa: E402
from neops_compose.doctor import LOGS_PROBE, NAME_WIDTH  # noqa: E402
from neops_compose.scenario import Scenario  # noqa: E402
from neops_compose.token import mint_access_token  # noqa: E402

CHECK_IDS = tuple(f"S{n}" for n in range(1, 14))
PASS, FAIL, NA = "PASS", "FAIL", "N/A"
DEFAULT_SCHEMA = REPO.parent / "neops-workflow-engine" / "schema" / "LogRecord.schema.json"
NOT_CONFIGURED = "LOGS_NOT_CONFIGURED"
CURSOR_REQUIRES_ASC = "CURSOR_REQUIRES_ASC"
REMOVED_ROUTE = "/blackboard/job/log"
OUTBOX_COUNT = "select count(*) from log_outbox"
LOG_SERVICES = ("engine", "cms", "worker")
LOG_WINDOW = "10m"
# A job polled before an upgrade reports without a lease; the engine refuses it on purpose.
TOLERATED_ERRORS = ("LEASE_MISSING",)
ERROR_WORD = re.compile(r"\b(ERROR|CRITICAL|FATAL)\b")
JSON_ERROR_LEVELS = frozenset({"error", "critical", "fatal"})
VIEWER = "e2e-smoke-viewer"
_VIEWER_CREATE = (
    "import os; from django.contrib.auth import get_user_model; "
    "u, _ = get_user_model().objects.get_or_create(username=os.environ['NEOPS_USERNAME']); "
    "u.set_unusable_password(); u.is_active = True; u.save(); u.roles.clear(); print('VIEWER')"
)
_VIEWER_DELETE = (
    "import os; from django.contrib.auth import get_user_model; "
    "get_user_model().objects.filter(username=os.environ['NEOPS_USERNAME']).delete()"
)


@dataclass(frozen=True)
class Check:
    id: str
    verdict: str
    detail: str

    def line(self) -> str:
        return f"{self.verdict:<4} {self.id:<3} {self.detail}"


def judged(check_id: str, ok: bool, detail: str) -> Check:
    return Check(check_id, PASS if ok else FAIL, detail)


def exit_code(checks: Iterable[Check]) -> int:
    return 1 if any(c.verdict == FAIL for c in checks) else 0


def doctor_verdict(code: int, stdout: str, expects_logs_probe: bool) -> tuple[bool, str]:
    """Doctor's own exit code, and its log-store line when compose.logs.yaml is on: `OK`, since
    a WARN there (an unreachable store) passes doctor and still is no green smoke."""
    line = next((ln for ln in stdout.splitlines() if ln[5 : 5 + NAME_WIDTH].strip() == LOGS_PROBE), None)
    if code != 0:
        return False, f"doctor exit {code}"
    if not expects_logs_probe:
        return True, "doctor exit 0; no log-store probe for this store"
    return bool(line and line.startswith("OK")), f"doctor exit 0; {line or 'no log-store probe line'}"


def version_verdict(logs: dict, has_store: bool) -> tuple[bool, str]:
    if not has_store:
        return logs.get("queryable") is False, f"queryable={logs.get('queryable')}"
    ok = logs.get("queryable") is True and logs.get("state") == "ok" and logs.get("entriesDropped") == 0
    detail = (
        f"queryable={logs.get('queryable')} state={logs.get('state')} dropped={logs.get('entriesDropped')}"
    )
    return ok, detail


def is_error_line(line: str) -> bool:
    """JSON lines by their level field, text lines by the level word; one pattern per format."""
    text = line.strip()
    if text.startswith("{"):
        try:
            record = json.loads(text)
        except ValueError:
            record = None
        if isinstance(record, dict):
            level = str(record.get("level") or record.get("levelname") or "").lower()
            return level in JSON_ERROR_LEVELS
    return bool(ERROR_WORD.search(text))


def error_lines(lines: Iterable[str]) -> list[str]:
    return [ln for ln in lines if is_error_line(ln) and not any(t in ln for t in TOLERATED_ERRORS)]


def line_formats(lines: list[str]) -> str:
    json_lines = sum(ln.lstrip().startswith("{") for ln in lines)
    return f"{json_lines} json/{len(lines) - json_lines} text"


@dataclass
class Smoke:
    deployment: workflows.Deployment
    engine: workflows.Engine
    args: argparse.Namespace
    store: logrecords.LogStore | None = None
    run: workflows.Run | None = None
    items: list[dict] = field(default_factory=list)

    @property
    def base(self) -> Path:
        return self.deployment.base

    @property
    def compose(self) -> Compose:
        return self.deployment.compose

    def doctor_flags(self) -> list[str]:
        flags = ["--connect", self.args.connect] if self.args.connect else []
        return flags + (["--insecure"] if self.args.insecure else [])

    def grafana_runs(self) -> bool:
        return "grafana" in self.compose.running_services()

    def store_row(self) -> dict | None:
        return next((r for r in self.compose.ps() if r.get("Service") == self.store.service), None)

    def s1(self) -> Check:
        out = neops(self.base, "doctor", *self.doctor_flags(), check=False, capture=True)
        print(out.stdout, flush=True)
        expects = Scenario.from_env(self.deployment.values).logs
        return judged("S1", *doctor_verdict(out.returncode, out.stdout, expects))

    def s2(self) -> Check:
        ok, detail = version_verdict(workflows.engine_logs_status(self.deployment), self.store is not None)
        if self.store is not None or not ok:
            return judged("S2", ok, detail)
        status, payload = workflows.run_logs(self.engine, str(uuid.uuid4()))
        refused = status == 503 and NOT_CONFIGURED in json.dumps(payload)
        return judged("S2", refused, f"{detail}; a log read answers {status} {str(payload)[:120]}")

    def s3(self) -> Check:
        if self.store is None:
            return Check("S3", NA, "no log store")
        row = self.store_row()
        if row is None:
            return judged("S3", False, f"no {self.store.service} container")
        networks = container_networks(row["Name"])
        wanted = [f"{row['Project']}_logs"]
        ok = networks == wanted and row.get("State") == "running"
        return judged("S3", ok, f"{row['Name']} {row.get('State')} on {networks}, want {wanted}")

    def s4(self) -> Check:
        if self.store is None:
            return Check("S4", NA, "no log store")
        isolated, why = inside.worker_isolated_from(self.base, self.store.ready_url)
        return judged("S4", isolated, why)

    def s5(self) -> Check:
        if self.store is None or not self.store.datasource_type or not self.grafana_runs():
            return Check("S5", NA, "no Grafana datasource for this store")
        types = grafana_datasource_types(self.base, self.deployment.values)
        return judged("S5", self.store.datasource_type in types, f"datasource types {types}")

    def s6(self) -> Check:
        if not self.grafana_runs():
            return Check("S6", NA, "no Grafana")
        count = len(grafana_dashboards(self.base, self.deployment.values))
        return judged(
            "S6", count >= self.args.min_dashboards, f"{count} dashboards, want >= {self.args.min_dashboards}"
        )

    def s7(self) -> Check:
        self.run = workflows.run_once(self.engine, workflows.prepare(self.deployment, self.engine))
        detail = f"run {self.run.execution_id} {self.run.status} in {self.run.seconds:.0f}s"
        if not self.run.succeeded or self.store is None:
            return judged("S7", self.run.succeeded, detail)
        lines, self.items = workflows.run_lines(self.engine, self.run.execution_id)
        self.dump_keys()
        return judged(
            "S7", not lines.problems(), f"{detail}; {lines}; {', '.join(lines.problems()) or 'complete'}"
        )

    def dump_keys(self) -> None:
        if self.args.dump_keys:
            self.args.dump_keys.write_text(json.dumps(logrecords.key_set(self.items), indent=2) + "\n")

    def s8(self) -> Check:
        if self.store is None or self.run is None:
            return Check("S8", NA, "no log store" if self.store is None else "no S7 run")
        run_id = self.run.execution_id
        status, payload = workflows.run_logs(
            self.engine, run_id, includeCore="true", level="warn", limit="500"
        )
        below = logrecords.level_problems(payload.get("items", [])) if status == 200 else [f"status {status}"]
        _, first = workflows.run_logs(self.engine, run_id, includeCore="true", limit="1")
        cursor = first.get("nextCursor") if isinstance(first, dict) else None
        if not cursor:
            return judged("S8", False, f"level=warn {below or 'ok'}; no cursor to test order=desc with")
        status, payload = workflows.run_logs(self.engine, run_id, after=cursor, order="desc")
        refused = status == 400 and CURSOR_REQUIRES_ASC in json.dumps(payload)
        return judged("S8", not below and refused, f"level=warn {below or 'ok'}; after+desc -> {status}")

    def s9(self) -> Check:
        target = self.run.execution_id if self.run else str(uuid.uuid4())
        try:
            self.compose.exec("cms", "python", "manage.py", "shell", "-c", _VIEWER_CREATE, env=viewer_env())
            viewer = workflows.Engine(self.deployment, lambda: mint_access_token(self.compose, VIEWER))
            status, _ = workflows.run_logs(viewer, target)
        finally:
            self.compose.exec("cms", "python", "manage.py", "shell", "-c", _VIEWER_DELETE, env=viewer_env())
        return judged("S9", status == 403, f"{VIEWER} (no workflow grant) reads run logs -> {status}")

    def s10(self) -> Check:
        out = self.compose.exec(
            "postgres-engine", "psql", "-U", "postgres", "-d", "neops-workflow", "-tAc", OUTBOX_COUNT
        )
        return judged("S10", out.strip() == "0", f"log_outbox rows: {out.strip()}")

    def s11(self) -> Check:
        status, body = inside.from_engine(self.base, "POST", inside.ENGINE_INTERNAL + REMOVED_ROUTE, "{}")
        return judged("S11", status == 404, f"POST {REMOVED_ROUTE} from inside the engine -> {status}")

    def s12(self) -> Check:
        if self.store is None or not self.items:
            return Check("S12", NA, "no S7 records to validate")
        if not self.args.record_schema.is_file():
            return judged("S12", False, f"no schema at {self.args.record_schema}: pass --record-schema")
        schema = json.loads(self.args.record_schema.read_text())
        errors = [e for item in self.items for e in logrecords.schema_errors(item, schema)]
        return judged("S12", not errors, f"{len(self.items)} records, {len(errors)} errors {errors[:3]}")

    def s13(self) -> Check:
        found, formats = {}, {}
        for service in LOG_SERVICES:
            lines = self.compose.run(
                "logs", "--no-log-prefix", "--since", LOG_WINDOW, service, capture=True
            ).stdout
            lines = lines.splitlines()
            formats[service] = line_formats(lines)
            found[service] = error_lines(lines)
        bad = {s: ls for s, ls in found.items() if ls}
        for service, lines in bad.items():
            print("\n".join(f"  {service}: {ln[:200]}" for ln in lines[:10]), flush=True)
        counts = ", ".join(f"{s} {len(found[s])} ({formats[s]})" for s in LOG_SERVICES)
        return judged("S13", not bad, f"error lines in the last {LOG_WINDOW}: {counts}")

    def checks(self) -> dict[str, Callable[[], Check]]:
        return {check_id: getattr(self, check_id.lower()) for check_id in CHECK_IDS}


def container_networks(name: str) -> list[str]:
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", name],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(json.loads(out))


def viewer_env() -> dict[str, str]:
    return {"NEOPS_USERNAME": VIEWER}


def run_checks(smoke: Smoke, only: list[str] | None) -> list[Check]:
    results = []
    for check_id, check in smoke.checks().items():
        if only and check_id not in only:
            continue
        try:
            result = check()
        except Exception as exc:  # one broken check must not hide the others
            result = Check(check_id, FAIL, f"raised {type(exc).__name__}: {exc}")
        print(result.line(), flush=True)
        results.append(result)
    return results


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, required=True, help="the deployment directory (holds .env)")
    ap.add_argument("--connect", help="connect to this address instead of resolving the public hostnames")
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed certificate")
    ap.add_argument("--min-dashboards", type=int, default=0, help="S6: the dashboard count before the deploy")
    ap.add_argument("--record-schema", type=Path, default=DEFAULT_SCHEMA, help="S12: LogRecord.schema.json")
    ap.add_argument("--dump-keys", type=Path, help="write the key set of the S7 records here")
    ap.add_argument("--only", action="append", choices=CHECK_IDS, help="run only this check (repeatable)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if refusal := shell_compose_refusal(os.environ, args.base):
        print(refusal, file=sys.stderr)
        return 2
    deployment = workflows.load_deployment(args.base.resolve(), args.connect, args.insecure)
    engine = workflows.Engine(deployment, lambda: workflows.admin_token(deployment))
    smoke = Smoke(deployment, engine, args, logrecords.store_of(deployment.values))
    results = run_checks(smoke, args.only)
    failed = [r.id for r in results if r.verdict == FAIL]
    print(
        f"\nsmoke: {len(results) - len(failed)}/{len(results)} checks passed or not applicable"
        + (f"; failed: {', '.join(failed)}" if failed else ""),
        flush=True,
    )
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
