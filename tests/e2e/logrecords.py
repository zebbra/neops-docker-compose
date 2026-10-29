"""What the e2e harness knows about log stores and log records, free of Docker and HTTP.

The smoke suite, the scenario runner and the chaos steps all judge records the engine's read
API returns; the rules they judge by live here so each is written once and unit-tested.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from neops_compose.rules import LOG_BACKEND
from neops_compose.scenario import Scenario

ENGINE_SERVICE = "neops-workflow-engine"
WORKER_SERVICE = "neops-worker"
CORE_SERVICE = "neops-core"
SERVICES = (ENGINE_SERVICE, WORKER_SERVICE, CORE_SERVICE)
DB_BACKEND = "db"
# Loki injects these at ingestion; the engine drops them on read and no record may carry them.
LOKI_INGEST_KEYS = frozenset({"service_name", "detected_level"})
WARN_LEVELS = frozenset({"warn", "error"})


@dataclass(frozen=True)
class LogStore:
    """A store the harness runs as a compose service, as the engine and Grafana reach it."""

    backend: str
    service: str
    url: str
    ready_path: str
    datasource_type: str | None
    ingest_keys: frozenset[str] = field(default=frozenset())

    @property
    def ready_url(self) -> str:
        return self.url + self.ready_path


STORES = {
    "victorialogs": LogStore(
        "victorialogs",
        "victorialogs",
        "http://victorialogs:9428",
        "/health",
        "victoriametrics-logs-datasource",
    ),
    "loki": LogStore("loki", "loki", "http://loki:3100", "/ready", None, LOKI_INGEST_KEYS),
}


def log_backend(values: Mapping[str, str]) -> str:
    """compose.logs.yaml sets the backend itself; without it the .env names the operator's own."""
    if Scenario.from_env(values).logs:
        return "victorialogs"
    return values.get(LOG_BACKEND) or DB_BACKEND


def store_of(values: Mapping[str, str]) -> LogStore | None:
    return STORES.get(log_backend(values))


def log_checks(values: Mapping[str, str]) -> tuple[str, ...]:
    """The log assertions that apply to one deployment, by the store it writes to."""
    store = store_of(values)
    if store is None:
        return ("not-queryable",)
    checks = ["store-running", "sole-store", "store-isolated", "engine-store-ok", "run-lines", "worker-lines"]
    if store.datasource_type:
        checks.append("grafana-datasource")
    if store.ingest_keys:
        checks.append("no-ingest-keys")
    return tuple(checks)


def other_store_services(store: LogStore) -> list[str]:
    """The services of every store this deployment does not write to: none of them may run."""
    return sorted(other.service for other in STORES.values() if other.service != store.service)


def ingest_key_problems(records: list[Mapping], keys: Collection[str]) -> list[str]:
    """Each store-injected key found on a record, at the top or among the producer's fields."""
    counts: Counter[str] = Counter()
    for record in records:
        present = set(record) | set(record.get("fields") or {})
        counts.update(present & set(keys))
    return [f"{key} on {n} of {len(records)} records" for key, n in sorted(counts.items())]


def seq_series_key(record: Mapping) -> tuple[str, ...]:
    """`seq` counts per process for the engine and core, and per job within a worker."""
    key = (str(record.get("service")), str(record.get("process")))
    if record.get("service") == WORKER_SERVICE:
        return (*key, str(record.get("job_id", "")))
    return key


def seq_problems(records: Iterable[Mapping], holes_in: Collection[str] = SERVICES) -> list[str]:
    """Duplicates in every series, and the missing numbers between the lowest and the highest
    `seq` seen in the series of the services in `holes_in`. A SIGKILLed engine loses its
    in-memory tail, and a store probe that fails takes an engine seq it never spools, so steps
    that do either leave the engine out of `holes_in`."""
    series: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for record in records:
        series[seq_series_key(record)].append(int(record["seq"]))
    problems = []
    for key, seqs in sorted(series.items()):
        counts = Counter(seqs)
        duplicates = sorted(s for s, n in counts.items() if n > 1)
        missing = sorted(set(range(min(counts), max(counts) + 1)) - set(counts))
        if duplicates:
            problems.append(f"{'/'.join(key)}: duplicate seq {_abbreviate(duplicates)}")
        if key[0] in holes_in and missing:
            problems.append(f"{'/'.join(key)}: {len(missing)} missing seq {_abbreviate(missing)}")
    return problems


def _abbreviate(numbers: list[int], shown: int = 8) -> str:
    head = ", ".join(map(str, numbers[:shown]))
    return head + (f" … ({len(numbers)} in all)" if len(numbers) > shown else "")


def key_set(records: Iterable[Mapping]) -> list[str]:
    """Every key a record carries, `fields.<k>` for the producer's own: what two stores must agree on."""
    keys: set[str] = set()
    for record in records:
        keys.update(k for k in record if k != "fields")
        keys.update(f"fields.{k}" for k in record.get("fields") or {})
    return sorted(keys)


# DbUpdater's attribution record, written once per change an unlock applies
# (neops-core locking/db_updater.py). Core writes no line for a lock it grants.
UNLOCK_AUDIT = re.compile(r"^lock .+?: applied \S+ on \S+ .* -- actor: ")


def is_unlock_audit(record: Mapping) -> bool:
    return record.get("service") == CORE_SERVICE and bool(UNLOCK_AUDIT.match(str(record.get("message", ""))))


WORKFLOW_IDENTITY = ("workflow_package", "workflow_name", "workflow_version")
FUNCTION_BLOCK_IDENTITY = ("function_block_package", "function_block_name", "function_block_version")


def identity_keys(record: Mapping) -> tuple[str, ...]:
    """The engine stamps a run's workflow on every engine and worker line of the run, and a
    job's function block on every line of the job, the worker's pushed ones included."""
    if record.get("service") not in (ENGINE_SERVICE, WORKER_SERVICE):
        return ()
    if record.get("job_id"):
        return WORKFLOW_IDENTITY + FUNCTION_BLOCK_IDENTITY
    if record.get("workflow_execution_id"):
        return WORKFLOW_IDENTITY
    return ()


def identity_problems(records: Iterable[Mapping]) -> list[str]:
    missing: Counter[tuple[str, str]] = Counter()
    for record in records:
        service = str(record.get("service"))
        missing.update((service, key) for key in identity_keys(record) if not record.get(key))
    return [f"{key} missing on {n} {service} lines" for (service, key), n in sorted(missing.items())]


@dataclass(frozen=True)
class RunLines:
    """What one run's log page holds, by producer."""

    engine: int
    worker: int
    core_unlock_audit: int
    foreign: int
    unidentified: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def count(cls, records: list[Mapping], execution_id: str) -> RunLines:
        own = [r for r in records if r.get("workflow_execution_id") == execution_id]
        return cls(
            engine=sum(r.get("service") == ENGINE_SERVICE for r in own),
            worker=sum(r.get("service") == WORKER_SERVICE for r in own),
            core_unlock_audit=sum(map(is_unlock_audit, own)),
            foreign=len(records) - len(own),
            unidentified=tuple(identity_problems(own)),
        )

    def problems(self) -> list[str]:
        wanted = {
            "engine": self.engine,
            "worker": self.worker,
            "core unlock audit": self.core_unlock_audit,
        }
        out = [f"no {name} line" for name, n in wanted.items() if n < 1]
        if self.foreign:
            out.append(f"{self.foreign} lines of another run")
        return out + list(self.unidentified)


def level_problems(records: Iterable[Mapping], minimum: frozenset[str] = WARN_LEVELS) -> list[str]:
    return [
        f"{r.get('level')}: {str(r.get('message'))[:80]}" for r in records if r.get("level") not in minimum
    ]


class SchemaError(ValueError):
    pass


def schema_errors(record: object, schema: Mapping) -> list[str]:
    """The draft-07 subset LogRecord.schema.json uses: no validator library ships with this repo."""
    return _errors(record, schema, schema, "$")


def _resolve(node: Mapping, root: Mapping) -> Mapping:
    ref = node.get("$ref")
    if ref is None:
        return node
    if not ref.startswith("#/definitions/"):
        raise SchemaError(f"unsupported $ref {ref}")
    return root["definitions"][ref.removeprefix("#/definitions/")]


TYPES = {
    "object": dict,
    "string": str,
    "integer": int,
    "boolean": bool,
    "array": list,
}


def _type_ok(value: object, name: str) -> bool:
    if name == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, TYPES[name])


def _errors(value: object, node: Mapping, root: Mapping, path: str) -> list[str]:
    node = _resolve(node, root)
    if "type" in node and not _type_ok(value, node["type"]):
        return [f"{path}: expected {node['type']}, got {type(value).__name__}"]
    out = _scalar_errors(value, node, path)
    if isinstance(value, dict):
        out += _object_errors(value, node, root, path)
    return out


def _scalar_errors(value: object, node: Mapping, path: str) -> list[str]:
    out = []
    if "const" in node and value != node["const"]:
        out.append(f"{path}: {value!r} is not {node['const']!r}")
    if "enum" in node and value not in node["enum"]:
        out.append(f"{path}: {value!r} is not one of {node['enum']}")
    if isinstance(value, str):
        if len(value) < node.get("minLength", 0):
            out.append(f"{path}: shorter than {node['minLength']}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            out.append(f"{path}: longer than {node['maxLength']}")
        if "pattern" in node and not re.search(node["pattern"], value):
            out.append(f"{path}: {value!r} does not match {node['pattern']}")
        if node.get("format") == "uuid" and not _is_uuid(value):
            out.append(f"{path}: {value!r} is not a uuid")
        if node.get("format") == "date-time" and not _is_date_time(value):
            out.append(f"{path}: {value!r} is not an RFC 3339 date-time")
    if isinstance(value, int) and "minimum" in node and value < node["minimum"]:
        out.append(f"{path}: below {node['minimum']}")
    return out


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _is_date_time(value: str) -> bool:
    if "T" not in value.upper():
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _object_errors(value: dict, node: Mapping, root: Mapping, path: str) -> list[str]:
    out = [f"{path}: missing {key}" for key in node.get("required", ()) if key not in value]
    if "maxProperties" in node and len(value) > node["maxProperties"]:
        out.append(f"{path}: more than {node['maxProperties']} properties")
    properties = node.get("properties", {})
    extra = node.get("additionalProperties", True)
    for key, item in value.items():
        out += _name_errors(key, node.get("propertyNames"), f"{path}.{key}")
        if key in properties:
            out += _errors(item, properties[key], root, f"{path}.{key}")
        elif extra is False:
            out.append(f"{path}: unexpected key {key}")
        elif isinstance(extra, dict):
            out += _errors(item, extra, root, f"{path}.{key}")
    return out


def _name_errors(name: str, rule: Mapping | None, path: str) -> list[str]:
    if not rule:
        return []
    out = _scalar_errors(name, {k: v for k, v in rule.items() if k != "not"}, path + " (name)")
    if name in rule.get("not", {}).get("enum", ()):
        out.append(f"{path}: reserved key")
    return out
