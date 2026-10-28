"""HTTP asked from inside a container of the stack, where the internal service names resolve.

The log stores publish no port and their images carry no shell or HTTP client, and the
engine's worker routes are denied at the public proxy, so the harness asks from the engine's
Node or the worker's Python instead.
"""

from __future__ import annotations

from pathlib import Path

from neops_compose.compose import Compose, ComposeError

ENGINE_INTERNAL = "http://127.0.0.1:3030"
NODE_GET = (
    "require('http').get(process.argv[1], r => process.exit(r.statusCode === 200 ? 0 : 1))"
    ".on('error', () => process.exit(1))"
)
PYTHON_GET = "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5)"
# Prints the status line and the body, so a caller sees both; exits 1 only when nothing answered.
NODE_REQUEST = (
    "const [method, url, body] = process.argv.slice(1);"
    "const r = require('http').request(url, {method, headers: {'content-type': 'application/json'}}, s => {"
    "let d = ''; s.on('data', c => d += c);"
    "s.on('end', () => { console.log(s.statusCode); console.log(d) }) });"
    "r.on('error', e => { console.log('error ' + e.message); process.exit(1) }); r.end(body || '');"
)


def reaches(base: Path, service: str, *cmd: str) -> bool:
    try:
        Compose(base).exec(service, *cmd)
    except ComposeError:
        return False
    return True


def engine_reaches(base: Path, url: str) -> bool:
    return reaches(base, "engine", "node", "-e", NODE_GET, url)


def worker_reaches(base: Path, url: str) -> bool:
    return reaches(base, "worker", "python", "-c", PYTHON_GET, url)


def parse_status(out: str) -> tuple[int | None, str]:
    first, _, body = out.partition("\n")
    return (int(first) if first.strip().isdigit() else None), body.strip()


def from_engine(base: Path, method: str, url: str, body: str = "") -> tuple[int | None, str]:
    """`url` as the engine container reaches it: (status, body), or (None, why) when nothing answered."""
    try:
        out = Compose(base).exec("engine", "node", "-e", NODE_REQUEST, method, url, body)
    except ComposeError as exc:
        return None, str(exc)
    return parse_status(out)
