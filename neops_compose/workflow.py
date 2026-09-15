from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from neops_compose import migrate, preflight, secrets, token
from neops_compose.compose import Compose
from neops_compose.doctor import all_ok as doctor_ok
from neops_compose.doctor import format_report as doctor_report
from neops_compose.doctor import run as run_doctor
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.render import render
from neops_compose.rotate import public_hosts
from neops_compose.scenario import Scenario
from neops_compose.state import State

CMS_FIRST = ("postgres-cms", "redis", "elasticsearch", "cms-init", "cms")


class Downgrade(RuntimeError):
    pass


class Blocked(RuntimeError):
    pass


@dataclass
class Ctx:
    repo: Path
    env: Env
    paths: Paths
    scenario: Scenario
    compose: Compose
    state: State
    log: Callable[[str], None]

    @classmethod
    def build(cls, repo: Path, log: Callable[[str], None]) -> Ctx:
        env = Env(repo / ".env")
        paths = Paths.for_repo(repo, env)
        return cls(repo, env, paths, Scenario.from_env(env), Compose(repo), State.load(paths.state_file), log)

    def save_state(self) -> None:
        self.state.save(self.paths.state_file)


def tag_of(image: str) -> str:
    return image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""


def _semver(tag: str) -> tuple | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([A-Za-z]+)\.(\d+))?$", tag)
    if not m:
        return None
    major, minor, patch, pre, pre_n = m.groups()
    return (int(major), int(minor), int(patch), 0 if pre is None else -1, int(pre_n or 0))


def is_downgrade(previous: str, current: str) -> bool:
    a, b = _semver(previous), _semver(current)
    if a is None or b is None:
        return False
    return b < a


def guard_downgrade(last_up: dict | None, images: dict[str, str], allow: bool) -> None:
    if not last_up or allow:
        return
    prev = last_up.get("images", {}).get("cms", "")
    now = images.get("cms", "")
    if "neops-core" in now and is_downgrade(tag_of(prev), tag_of(now)):
        raise Downgrade(
            f"neops-core would move from {tag_of(prev)} back to {tag_of(now)}; Django migrations "
            "are not reversible. Restore a backup instead, or pass --allow-downgrade if you know better."
        )


def images_by_service(compose: Compose) -> dict[str, str]:
    out = {}
    for row in compose.ps():
        if row.get("Image"):
            out[row["Service"]] = row["Image"]
    return out


def check(ctx: Ctx, check_images: bool = True) -> None:
    checks = preflight.run_checks(ctx.env, ctx.scenario, ctx.repo, ctx.paths.data, ctx.compose, check_images)
    ctx.log(preflight.format_report(checks))
    if not preflight.all_ok(checks):
        raise Blocked("preflight failed; fix the FAIL lines above")
    if ctx.state.written_by_newer_cli():
        raise Blocked(
            f"data/ was last written by CLI {ctx.state.cli}, newer than this checkout; git pull first"
        )


def _selfsigned_keys(ctx: Ctx) -> None:
    hosts = public_hosts(ctx.env, ctx.scenario)
    cert = ctx.paths.tls_dir / "cert.pem"
    if cert.exists():
        missing = secrets.stale_sans(cert, hosts)
        if missing:
            raise Blocked(
                f"the self-signed certificate lacks {', '.join(sorted(missing))}; run ./neops rotate tls"
            )
    elif secrets.ensure_selfsigned(ctx.paths.tls_dir, hosts):
        ctx.log(f"generated a self-signed certificate for {', '.join(hosts)}")


def keys(ctx: Ctx) -> None:
    if secrets.ensure_jwt(ctx.paths.jwt_dir):
        ctx.log("generated the JWT keypair")
    if ctx.scenario.keycloak:
        secrets.ensure_keycloak_client_secret(ctx.paths.keycloak_client_env)
    if ctx.env.flag("NEOPS_TLS_SELF_SIGNED"):
        _selfsigned_keys(ctx)


def migrate_all(ctx: Ctx, dry_run: bool = False) -> list[str]:
    return migrate.apply_all(ctx.env, ctx.paths, ctx.state, ctx.log, dry_run=dry_run)


def doctor(
    ctx: Ctx, connect: str | None = None, insecure: bool = False, probe_ratelimit: bool = False
) -> bool:
    probes = run_doctor(ctx.env, ctx.scenario, ctx.compose, connect, insecure, probe_ratelimit)
    ctx.log(doctor_report(probes))
    return doctor_ok(probes)


def _finish(ctx: Ctx, connect: str | None, insecure: bool) -> None:
    ctx.state.record_up(images_by_service(ctx.compose))
    ctx.save_state()
    if not doctor(ctx, connect, insecure):
        raise Blocked("the stack is up but doctor reports failures")


def install(ctx: Ctx, connect: str | None = None, insecure: bool = False) -> None:
    check(ctx)
    migrate_all(ctx)
    keys(ctx)
    render(ctx.env, ctx.scenario, ctx.paths)
    ctx.log("pulling images")
    ctx.compose.pull()
    ctx.log("starting the CMS")
    ctx.compose.up(*CMS_FIRST)
    token.ensure_engine_token(ctx.compose, ctx.env, ctx.paths, ctx.state, ctx.log)
    ctx.log("starting everything")
    ctx.compose.up()
    _finish(ctx, connect, insecure)


def up(ctx: Ctx, allow_downgrade: bool = False, connect: str | None = None, insecure: bool = False) -> None:
    check(ctx, check_images=False)
    migrate_all(ctx)
    keys(ctx)
    render(ctx.env, ctx.scenario, ctx.paths)
    ctx.compose.pull()
    core_image = next((i for i in ctx.compose.images() if "neops-core" in i), "")
    guard_downgrade(ctx.state.last_up, {"cms": core_image}, allow_downgrade)
    ctx.compose.up()
    if token.ensure_engine_token(ctx.compose, ctx.env, ctx.paths, ctx.state, ctx.log):
        ctx.compose.up()
    _finish(ctx, connect, insecure)


def purge(ctx: Ctx, confirmed: str) -> None:
    expected = str(ctx.paths.data)
    if confirmed != expected:
        raise Blocked(f"purge removes every container and {expected}; re-run with --confirm {expected}")
    ctx.compose.down()
    for p in (ctx.paths.data, ctx.paths.generated):
        if p.exists():
            shutil.rmtree(p)
    ctx.log(f"removed {ctx.paths.data} and {ctx.paths.generated}; .env, certs/ and backups/ were kept")


def status(ctx: Ctx) -> str:
    lines = [f"scenario: {' : '.join(ctx.scenario.files)}", f"data: {ctx.paths.data}"]
    running = images_by_service(ctx.compose)
    lines.append("images (pinned):")
    lines += [f"  {img}" for img in ctx.compose.images()]
    lines.append("images (running):")
    lines += [f"  {svc}: {img}" for svc, img in sorted(running.items())] or ["  none"]
    pend = migrate.pending(migrate.discover(ctx.paths.migrations), ctx.state)
    faked = f", {len(ctx.state.faked)} FAKED ({', '.join(ctx.state.faked_names)})" if ctx.state.faked else ""
    lines.append(f"migrations: {len(ctx.state.applied)} applied, {len(pend)} pending" + faked)
    if ctx.state.last_up:
        lines.append(f"last successful up: {ctx.state.last_up['at']}")
    return "\n".join(lines)
