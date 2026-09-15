from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from packaging.version import InvalidVersion, Version

from neops_compose import migrate, passwords, preflight, secrets, token
from neops_compose.compose import Compose
from neops_compose.context import Ctx
from neops_compose.doctor import all_ok as doctor_ok
from neops_compose.doctor import format_report as doctor_report
from neops_compose.doctor import run as run_doctor
from neops_compose.ownership import chown_via_container
from neops_compose.render import render
from neops_compose.rotate import public_hosts

CMS_FIRST = ("postgres-cms", "redis", "elasticsearch", "cms-init", "cms")
WORKER_GRACE_SECONDS = 90  # the worker registers ~26 function blocks, one request each, after a start


class Downgrade(RuntimeError):
    pass


class Blocked(RuntimeError):
    pass


def tag_of(image: str) -> str:
    return image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""


def is_downgrade(previous: str, current: str) -> bool:
    """An unreadable tag on either side is never a downgrade: only refuse what we understand."""
    try:
        return Version(current) < Version(previous)
    except InvalidVersion:
        return False


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


def check_images(ctx: Ctx) -> None:
    """Separate from check(): resolving the image list needs generated/, which render writes."""
    checks = preflight.image_checks(ctx.compose)
    ctx.log(preflight.format_report(checks))
    if not preflight.all_ok(checks):
        raise Blocked("preflight failed; fix the FAIL lines above")


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


def fill_secrets(ctx: Ctx) -> None:
    if not ctx.env.exists:
        raise Blocked("no .env file: copy one of examples/*.env to .env first")
    filled = passwords.fill_missing(ctx.env, ctx.scenario)
    if not filled:
        ctx.log("every generated secret in .env is already set")
        return
    ctx.log("generated " + ", ".join(filled) + " in .env")
    if "NEOPS_ADMIN_PASSWORD" in filled:
        ctx.log("the first login is NEOPS_ADMIN_USER with the NEOPS_ADMIN_PASSWORD now in .env")


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
    ctx: Ctx,
    connect: str | None = None,
    insecure: bool = False,
    probe_ratelimit: bool = False,
    worker_grace: float = 0,
) -> bool:
    probes = run_doctor(ctx, connect, insecure, probe_ratelimit, worker_grace)
    ctx.log(doctor_report(probes))
    return doctor_ok(probes)


def _finish(ctx: Ctx, connect: str | None, insecure: bool) -> None:
    """The images are recorded before doctor runs: whatever doctor does, the next up must
    compare against what is deployed now, not against the release before it."""
    ctx.state.record_up(images_by_service(ctx.compose), doctor_ok=False)
    ctx.save_state()
    if not doctor(ctx, connect, insecure, worker_grace=WORKER_GRACE_SECONDS):
        raise Blocked("the stack is up but doctor reports failures")
    ctx.state.record_doctor(True)
    ctx.save_state()


def _start(ctx: Ctx) -> None:
    """The engine refuses to boot on its placeholder token, and `up --wait` would sit on that
    restart loop until its timeout. So while no token exists yet the CMS comes up alone and
    mints one, and everything else starts after. A token minted later reaches the engine only
    through a restart, hence the second `up` whenever one was minted."""
    if token.read_engine_token(ctx.paths) is None:
        ctx.log("starting the CMS")
        ctx.compose.up(*CMS_FIRST)
    else:
        ctx.compose.up()
    if token.ensure_engine_token(ctx):
        ctx.log("starting everything")
        ctx.compose.up()


def install(ctx: Ctx, connect: str | None = None, insecure: bool = False) -> None:
    check(ctx, check_images=False)
    migrate_all(ctx)
    keys(ctx)
    render(ctx.env, ctx.scenario, ctx.paths)
    check_images(ctx)
    ctx.log("pulling images")
    ctx.compose.pull()
    _start(ctx)
    _finish(ctx, connect, insecure)


def _guard_downgrade(ctx: Ctx, allow_downgrade: bool) -> None:
    """Nothing to compare before the first successful start, and asking compose for the
    images then would fail anyway: generated/ does not exist until render has run."""
    if ctx.state.last_up is None:
        return
    core_image = next((i for i in ctx.compose.images() if "neops-core" in i), "")
    guard_downgrade(ctx.state.last_up, {"cms": core_image}, allow_downgrade)


def up(ctx: Ctx, allow_downgrade: bool = False, connect: str | None = None, insecure: bool = False) -> None:
    check(ctx, check_images=False)
    _guard_downgrade(ctx, allow_downgrade)
    migrate_all(ctx)
    keys(ctx)
    render(ctx.env, ctx.scenario, ctx.paths)
    ctx.compose.pull()
    _start(ctx)
    _finish(ctx, connect, insecure)


def purge(ctx: Ctx, confirmed: str) -> None:
    expected = str(ctx.paths.data)
    if confirmed != expected:
        raise Blocked(f"purge removes every container and {expected}; re-run with --confirm {expected}")
    ctx.compose.down()
    _take_ownership(ctx.paths.data, ctx.log)
    for p in (ctx.paths.data, ctx.paths.generated):
        if p.exists():
            shutil.rmtree(p)
    ctx.log(f"removed {ctx.paths.data} and {ctx.paths.generated}; .env, certs/ and backups/ were kept")


def _take_ownership(data: Path, log: Callable[[str], None]) -> None:
    """Postgres writes as uid 70 at mode 0700, Grafana as 472, Elasticsearch as 1000, so
    the operator cannot delete what the containers left without claiming it back first."""
    if not data.exists():
        return
    log(f"claiming {data} back from the containers that wrote it")
    chown_via_container(data, os.getuid(), os.getgid())


def _verdict(last_up: dict) -> str:
    recorded = last_up.get("doctor_ok")
    if recorded is None:
        return "doctor: not recorded"
    return "doctor ok" if recorded else "doctor FAILED"


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
        lines.append(f"last started: {ctx.state.last_up['at']} ({_verdict(ctx.state.last_up)})")
    return "\n".join(lines)
