#!/usr/bin/env python3
"""Put a real Caddy in front of an external-proxy stack and assert the documented contract.

    uv run python tests/e2e/run_scenario.py external-proxy --port-base 22000 \
        --workdir /tmp/neops-e2e/proxy --keep --extra-env NEOPS_ES_HEAP=512m
    uv run python tests/e2e/external_proxy_check.py /tmp/neops-e2e/proxy/repo --caddy-port 22443

It runs `caddy:2` from tests/e2e/caddy/Caddyfile, which is docs/40-external-proxy.md's snippet
with the harness's hostnames and ports, repoints the clone's public URLs at it, and checks the
four promises the docs make: doctor is fully green including the worker-API deny probe, the
engine's worker routes answer 403 while everything else still reaches the engine, the proxy
overwrites X-Real-IP, and a body far above the usual proxy default reaches the CMS.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

E2E = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E))

from run_scenario import CONNECT, Report, failing_probe_names, neops  # noqa: E402

from neops_compose.compose import Compose  # noqa: E402
from neops_compose.doctor import Http  # noqa: E402
from neops_compose.env import Env  # noqa: E402
from neops_compose.urls import PublicUrl  # noqa: E402

CADDYFILE = E2E / "caddy" / "Caddyfile"
CADDY_IMAGE = "caddy:2"
BODY_LIMIT_BYTES = 200 * 1000 * 1000  # what go-humanize makes of the Caddyfile's `200MB`
RATELIMIT_KEY = "RATELIMIT_IP_META_KEY"
RATELIMIT_VALUE = "HTTP_X_REAL_IP"
# Core allows five logins a minute per address and the run before this one spends some of
# them; the probe needs a fresh window or its own attempts cannot be what trips the limit.
RATELIMIT_WINDOW = 65.0
LOGIN = "mutation($u:String!,$p:String!){login(username:$u,password:$p){accessToken}}"

UPSTREAM_PORTS = {
    "NEOPS_WEB_PORT": "8080",
    "NEOPS_CMS_PORT": "8000",
    "NEOPS_ENGINE_PORT": "3030",
    "NEOPS_MONITOR_PORT": "3031",
}
HOST_OF_URL = {
    "NEOPS_WEB_HOST": "NEOPS_WEB_URL",
    "NEOPS_CMS_HOST": "NEOPS_CMS_URL",
    "NEOPS_ENGINE_HOST": "NEOPS_ENGINE_URL",
    "NEOPS_WORKFLOWS_HOST": "NEOPS_WORKFLOWS_URL",
}
# Every spelling Express accepts for the routes neops_compose/routes.py says must never be
# reachable from outside: case-insensitive, optional trailing slash, wildcard job paths.
DENIED = (
    "/blackboard/job",
    "/blackboard/job/",
    "/blackboard/job/abc-123",
    "/BlackBoard/Job",
    "/workers/register",
    "/Workers/Register/",
    "/workers/w-1/ping",
    "/workers/w-1/unregister/",
    "/function-blocks/register",
    "/Function-Blocks/Register",
)
# The monitor app needs these, so the deny list must not be a wholesale block of the engine.
REACHABLE = ("/health", "/workers", "/function-blocks/registrations/list", "/blackboard/jobs")
# The deny rule is POST-only and exact: none of these is a worker route.
NOT_DENIED = (("GET", "/workers/register"), ("GET", "/blackboard/job"), ("POST", "/workers"))


def docker(*args: str, check: bool = True) -> str:
    print("+ docker " + " ".join(args), flush=True)
    result = subprocess.run(["docker", *args], text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise SystemExit(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def caddy_environment(env: Env, port: int) -> dict[str, str]:
    values = {"NEOPS_PROXY_PORT": str(port), "NEOPS_UPSTREAM_HOST": CONNECT}
    values |= {key: env.get(key, default) for key, default in UPSTREAM_PORTS.items()}
    values |= {key: PublicUrl.parse(env.require(url)).host for key, url in HOST_OF_URL.items()}
    return values


def start_caddy(env: Env, port: int, name: str) -> dict[str, str]:
    """Host networking: the stack publishes on 127.0.0.1, which no bridged container reaches."""
    docker("rm", "-f", name, check=False)
    values = caddy_environment(env, port)
    flags = [flag for key, value in values.items() for flag in ("-e", f"{key}={value}")]
    docker(
        "run", "-d", "--name", name, "--network", "host",
        *flags,
        "-v", f"{CADDYFILE}:/etc/caddy/Caddyfile:ro",
        CADDY_IMAGE,
    )  # fmt: skip
    return values


def wait_for_caddy(http: Http, web: PublicUrl, name: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            http.request(web, "/")
            return
        except Exception:
            time.sleep(2)
    raise SystemExit(f"caddy never answered on {web}:\n{docker('logs', '--tail', '40', name)}")


def point_urls_at_caddy(env: Env, port: int) -> None:
    for key in HOST_OF_URL.values():
        url = PublicUrl.parse(env.require(key))
        env.set(key, f"https://{url.host}:{port}{url.path}")


def bad_login(http: Http, cms: PublicUrl, real_ip: str | None = None) -> tuple[int, str]:
    body = json.dumps({"query": LOGIN, "variables": {"u": "neops-proxy-check", "p": "wrong"}})
    headers = {"X-Real-IP": real_ip} if real_ip else None
    return http.request(cms, "/graphql", method="POST", body=body, headers=headers)


def assert_doctor_green(report: Report, clone: Path, label: str, *extra: str) -> None:
    names = failing_probe_names(clone, *extra)
    report.add(not names, f"{label}: every doctor probe passes (failing: {names or 'none'})")


def _denied_by_proxy(status: int, text: str) -> bool:
    """Caddy answers its own 403 with an empty body; anything the engine wrote has one."""
    return status == 403 and not text.strip()


def assert_deny(report: Report, engine: PublicUrl, http: Http) -> None:
    leaks = []
    for path in DENIED:
        status, text = http.request(engine, path, method="POST", body="{}")
        if not _denied_by_proxy(status, text):
            leaks.append(f"POST {path} -> {status} {text[:60]!r}")
    report.add(not leaks, f"every worker route is denied by the proxy ({leaks or 'all 403, empty body'})")

    missing = []
    for path in REACHABLE:
        status, text = http.request(engine, path)
        if status == 404 or _denied_by_proxy(status, text):
            missing.append(f"GET {path} -> {status}")
    report.add(not missing, f"the routes the monitor app needs reach the engine ({missing or 'all of them'})")

    over = []
    for method, path in NOT_DENIED:
        body = "{}" if method == "POST" else None
        status, text = http.request(engine, path, method=method, body=body)
        if _denied_by_proxy(status, text):
            over.append(f"{method} {path} -> 403 from the proxy")
    report.add(not over, f"the deny rule is POST-only and exact ({over or 'nothing over-matched'})")


def assert_body_limit(report: Report, cms: PublicUrl, http: Http, name: str) -> None:
    """1 MB is above nginx's default and every other common one; 200 MB is asserted on the
    adapted config rather than by sending it."""
    body = json.dumps({"query": "# " + "x" * 1_000_000 + "\n{__typename}"})
    status, text = http.request(cms, "/graphql", method="POST", body=body)
    answered_by_core = status == 200 and text.lstrip().startswith("{")
    report.add(answered_by_core, f"a 1 MB body reaches the CMS, nothing truncates it ({status}: {text[:80]})")
    adapted = docker("exec", name, "caddy", "adapt", "--config", "/etc/caddy/Caddyfile")
    report.add(
        f'"max_size":{BODY_LIMIT_BYTES}' in adapted.replace(" ", ""),
        f"the CMS site configures a {BODY_LIMIT_BYTES}-byte body limit",
    )


def assert_key_reaches_core(report: Report, clone: Path) -> bool:
    """A key core never receives makes --probe-ratelimit pass without testing anything."""
    try:
        seen = Compose(clone).exec("cms", "printenv", RATELIMIT_KEY).strip()
    except Exception as exc:
        seen = f"<{exc}>"
    return report.add(seen == RATELIMIT_VALUE, f"the cms container sees {RATELIMIT_KEY} (got {seen!r})")


def assert_ratelimit(report: Report, clone: Path, cms: PublicUrl, http: Http) -> None:
    Env(clone / ".env").set(RATELIMIT_KEY, RATELIMIT_VALUE)
    neops(clone, "up", "--connect", CONNECT, "--insecure", check=False)
    if not assert_key_reaches_core(report, clone):
        return
    time.sleep(RATELIMIT_WINDOW)
    assert_doctor_green(report, clone, "with the rate-limit key set", "--probe-ratelimit")
    status, text = bad_login(http, cms, real_ip="1.2.3.4")
    report.add(
        status == 429 or "Too many" in text,
        f"a forged X-Real-IP does not buy a fresh rate-limit bucket ({status}: {text[:120]})",
    )


def run(clone: Path, port: int, name: str) -> int:
    env = Env(clone / ".env")
    values = start_caddy(env, port, name)
    print("caddy " + name + ": " + ", ".join(f"{k}={v}" for k, v in values.items()), flush=True)

    point_urls_at_caddy(env, port)
    neops(clone, "render")
    http = Http(connect=CONNECT, insecure=True)
    urls = {key: PublicUrl.parse(env.require(key)) for key in HOST_OF_URL.values()}
    wait_for_caddy(http, urls["NEOPS_WEB_URL"], name)
    neops(clone, "up", "--connect", CONNECT, "--insecure", check=False)

    report = Report()
    assert_doctor_green(report, clone, "behind caddy")
    assert_deny(report, urls["NEOPS_ENGINE_URL"], http)
    assert_body_limit(report, urls["NEOPS_CMS_URL"], http, name)
    assert_ratelimit(report, clone, urls["NEOPS_CMS_URL"], http)

    passed = len(report.results) - len(report.failed)
    print(f"\nexternal proxy: {passed}/{len(report.results)} assertions passed", flush=True)
    for message in report.failed:
        print(f"  FAILED: {message}", flush=True)
    return 1 if report.failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clone", type=Path, help="the clone a --keep external-proxy run left behind")
    ap.add_argument("--caddy-port", type=int, default=22443)
    ap.add_argument("--keep", action="store_true", help="leave the caddy container running")
    args = ap.parse_args(argv)

    name = f"neops-e2e-caddy-{args.caddy_port}"
    try:
        return run(args.clone.resolve(), args.caddy_port, name)
    finally:
        if args.keep:
            print(f"caddy left running as {name}", flush=True)
        else:
            docker("rm", "-f", name, check=False)


if __name__ == "__main__":
    sys.exit(main())
