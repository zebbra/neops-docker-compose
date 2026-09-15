from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from neops_compose import __version__, backup, migrate, rotate, state, token, workflow
from neops_compose.compose import ComposeError
from neops_compose.context import Ctx
from neops_compose.env import MissingEnv
from neops_compose.render import MissingSecret, RenderError, render

REPO = Path(__file__).resolve().parent.parent
ERRORS = (
    workflow.Blocked,
    workflow.Downgrade,
    MissingEnv,
    MissingSecret,
    RenderError,
    ComposeError,
    migrate.MigrationError,
    rotate.RotateError,
    state.StateError,
    token.TokenCheckUnavailable,
)


def log(message: str) -> None:
    print(message, flush=True)


def _add_start_arguments(sp: argparse.ArgumentParser, name: str) -> None:
    sp.add_argument(
        "--connect", help="connect to this address instead of resolving the public hostnames (doctor)"
    )
    sp.add_argument("--insecure", action="store_true", help="skip TLS verification in doctor (self-signed)")
    if name == "up":
        sp.add_argument("--allow-downgrade", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="neops", description="Operate the NeOps 2.0 docker-compose deployment.")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_)

    for name, help_ in (
        ("install", "first-time installation from a filled .env"),
        ("up", "apply .env and repo changes: migrate, render, pull, start, doctor"),
    ):
        _add_start_arguments(add(name, help_), name)
    add("down", "stop the stack (data is kept)")
    add("ps", "container status")
    sp = add("logs", "follow logs")
    sp.add_argument("services", nargs="*")
    sp = add("restart", "restart services")
    sp.add_argument("services", nargs="+")
    sp = add(
        "compose", "run docker compose with this deployment's COMPOSE_FILE (prints secrets with `config`)"
    )
    sp.add_argument("args", nargs=argparse.REMAINDER)
    sp = add("check", "preflight: host, .env, scenario, images")
    sp.add_argument("--no-images", action="store_true")
    sp = add("migrate", "apply pending deployment migrations")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--fake", metavar="NAME", help="mark a migration applied without running it (full name)")
    add("keys", "generate missing key material (never overwrites)")
    add("token", "mint the engine's CMS API key when missing or invalid")
    sp = add("render", "regenerate generated/ from .env")
    sp.add_argument("--diff", action="store_true", help="show what would change")
    sp = add("doctor", "health report through the public URLs")
    sp.add_argument("--connect")
    sp.add_argument("--insecure", action="store_true")
    sp.add_argument(
        "--probe-ratelimit",
        action="store_true",
        help="also verify the proxy overwrites X-Real-IP (uses the login rate limit)",
    )
    add("status", "pinned vs running images, migrations, scenario")
    sp = add("backup", "logical backup into backups/<timestamp>/")
    sp.add_argument("--dir", type=Path)
    sp.add_argument("--keep", type=int, help="prune to the newest N archives")
    sp = add("rotate", "rotate a secret: " + ", ".join(rotate.WHAT))
    sp.add_argument("what", choices=rotate.WHAT)
    sp.add_argument("--which", choices=("cms", "engine", "keycloak"), help="for db-password")
    sp.add_argument("--password", help="for admin-password (prompted when omitted)")
    sp = add("purge", "stop everything and delete the data directory")
    sp.add_argument("--confirm", default="", metavar="DATA_DIR")
    add("version", "print the CLI version")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    try:
        return dispatch(args, Ctx.build(REPO, log))
    except ERRORS as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def dispatch(args: argparse.Namespace, ctx: Ctx) -> int:
    c = ctx.compose
    match args.command:
        case "install":
            workflow.install(ctx, args.connect, args.insecure)
        case "up":
            workflow.up(ctx, args.allow_downgrade, args.connect, args.insecure)
        case "down":
            c.down()
        case "ps":
            c.run("ps", "-a")
        case "logs":
            c.run("logs", "-f", "--tail", "200", *args.services, check=False)
        case "restart":
            c.run("restart", *args.services)
        case "compose":
            extra = args.args[1:] if args.args[:1] == ["--"] else args.args
            return c.run(*extra, check=False).returncode
        case "check":
            workflow.check(ctx, check_images=not args.no_images)
        case "migrate":
            return _migrate(args, ctx)
        case "keys":
            workflow.keys(ctx)
        case "token":
            token.ensure_engine_token(c, ctx.env, ctx.paths, ctx.state, log)
        case "render":
            _render(args, ctx)
        case "doctor":
            return 0 if workflow.doctor(ctx, args.connect, args.insecure, args.probe_ratelimit) else 1
        case "status":
            print(workflow.status(ctx))
        case "backup":
            _backup(args, ctx)
        case "rotate":
            _rotate(args, ctx)
        case "purge":
            workflow.purge(ctx, args.confirm)
    return 0


def _migrate(args: argparse.Namespace, ctx: Ctx) -> int:
    if not args.fake:
        done = workflow.migrate_all(ctx, dry_run=args.dry_run)
        prefix = "would apply: " if args.dry_run else "applied: "
        log("nothing to apply" if not done else prefix + ", ".join(done))
        return 0
    print(
        f"faking {args.fake}: it will be recorded as applied WITHOUT running. "
        "Type the name again to confirm: ",
        end="",
    )
    if input().strip() != args.fake:
        return 1
    migrate.fake(args.fake, ctx.paths, ctx.state)
    return 0


def _backup(args: argparse.Namespace, ctx: Ctx) -> None:
    target = backup.create(ctx, args.dir)
    if args.keep:
        for removed in backup.prune(target.parent, args.keep):
            log(f"pruned {removed}")


def _render(args: argparse.Namespace, ctx: Ctx) -> None:
    if args.diff:
        _render_diff(ctx)
        return
    for path in render(ctx.env, ctx.scenario, ctx.paths):
        log(f"wrote {path.relative_to(ctx.repo)}")


def _render_diff(ctx: Ctx) -> None:
    import difflib
    import shutil
    import tempfile

    from neops_compose.paths import Paths

    before = (
        {p: p.read_text() for p in ctx.paths.generated.rglob("*") if p.is_file()}
        if ctx.paths.generated.exists()
        else {}
    )
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "repo"
        shutil.copytree(
            ctx.repo, scratch, ignore=shutil.ignore_patterns("data", "backups", ".venv", ".git", "generated")
        )
        alt = Paths(repo=scratch, data=ctx.paths.data)
        render(ctx.env, ctx.scenario, alt)
        after = {
            ctx.paths.generated / p.relative_to(alt.generated): p.read_text()
            for p in alt.generated.rglob("*")
            if p.is_file()
        }
    for path in sorted(set(before) | set(after)):
        a, b = before.get(path, "").splitlines(), after.get(path, "").splitlines()
        diff = difflib.unified_diff(
            a, b, f"generated/{path.name} (current)", f"generated/{path.name} (rendered)", lineterm=""
        )
        for line in diff:
            print(line)


def _rotate(args: argparse.Namespace, ctx: Ctx) -> None:
    match args.what:
        case "db-password":
            if not args.which:
                raise workflow.Blocked("rotate db-password needs --which cms|engine|keycloak")
            rotate.db_password(ctx, args.which)
        case "admin-password":
            rotate.admin_password(ctx, args.password or getpass.getpass("new admin password: "))
        case "secret-key":
            rotate.secret_key(ctx)
        case "jwt":
            rotate.jwt(ctx)
        case "tls":
            rotate.tls(ctx)
        case "token":
            token.rotate_engine_token(ctx.compose, ctx.env, ctx.paths, ctx.state, log)
        case "keycloak-client":
            rotate.keycloak_client(ctx)


if __name__ == "__main__":
    sys.exit(main())
