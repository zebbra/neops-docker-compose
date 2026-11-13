from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from neops_compose import __version__, backup, databases, migrate, override, rotate, state, token, workflow
from neops_compose.compose import ComposeError
from neops_compose.context import Ctx
from neops_compose.env import MissingEnv
from neops_compose.ownership import OwnershipError
from neops_compose.render import MissingSecret, RenderError, render
from neops_compose.rules import BadPorts
from neops_compose.scenario import OVERRIDE_FILE
from neops_compose.urls import BadUrl

REPO = Path(__file__).resolve().parent.parent
APPLY_HINT = "takes effect with the next ./neops up"
# Every exception this package defines: each one names something the operator did or a
# state their deployment is in, so all of them belong in `error: ...` rather than a traceback.
ERRORS = (
    workflow.Blocked,
    workflow.Downgrade,
    MissingEnv,
    MissingSecret,
    RenderError,
    ComposeError,
    migrate.MigrationError,
    OwnershipError,
    rotate.RotateError,
    state.StateError,
    token.TokenCheckUnavailable,
    BadPorts,
    BadUrl,
    override.OverrideError,
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
    p = argparse.ArgumentParser(prog="neops", description="Operate the Neops 2.0 docker-compose deployment.")
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
    _add_config_parser(add("config", "core settings of your own, kept in compose.override.yaml"))
    sp = add("check", "preflight: host, .env, scenario, images")
    sp.add_argument("--no-images", action="store_true")
    sp = add("migrate", "apply pending deployment migrations")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--fake", metavar="NAME", help="mark a migration applied without running it (full name)")
    add("secrets", "fill every blank generated secret in .env (never overwrites)")
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
    sp.add_argument("--which", choices=tuple(databases.BY_KEY), help="for db-password")
    sp.add_argument("--password", help="for admin-password (prompted when omitted)")
    sp = add("purge", "stop everything and delete the data directory")
    sp.add_argument("--confirm", default="", metavar="DATA_DIR")
    add("version", "print the CLI version")
    return p


def _add_config_parser(sp: argparse.ArgumentParser) -> None:
    actions = sp.add_subparsers(dest="action", required=True)
    actions.add_parser("set", help="set core environment variables").add_argument(
        "assignments", nargs="+", metavar="KEY=VALUE"
    )
    actions.add_parser("unset", help="remove them again").add_argument("keys", nargs="+", metavar="KEY")
    actions.add_parser("list", help="show what compose.override.yaml sets for core")


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
        case "config":
            _config(args, ctx)
        case "check":
            workflow.check(ctx, check_images=not args.no_images)
        case "migrate":
            return _migrate(args, ctx)
        case "secrets":
            workflow.fill_secrets(ctx)
        case "keys":
            workflow.keys(ctx)
        case "token":
            token.ensure_engine_token(ctx)
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
        case _:
            raise SystemExit(f"unhandled command {args.command}")
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


def _config(args: argparse.Namespace, ctx: Ctx) -> None:
    match args.action:
        case "set":
            _config_set(args.assignments, ctx)
        case "unset":
            _config_unset(args.keys, ctx)
        case "list":
            _config_list(ctx)


def _config_set(assignments: list[str], ctx: Ctx) -> None:
    values = override.parse_assignments(assignments)
    override.require_dot_env(ctx.env)
    override.set_settings(ctx.repo, values)
    if override.ensure_listed(ctx.env):
        log(f"added {OVERRIDE_FILE} to COMPOSE_FILE in .env")
    services = ", ".join(override.core_services(ctx.repo))
    for key, value in values.items():
        log(f"set {key}={value} for {services}")
    log(APPLY_HINT)


def _config_unset(keys: list[str], ctx: Ctx) -> None:
    removed = override.unset_settings(ctx.repo, keys)
    for key in keys:
        log(f"removed {key}" if key in removed else f"{key} was not set")
    if removed:
        log(APPLY_HINT)


def _config_list(ctx: Ctx) -> None:
    current = override.settings(ctx.repo)
    if not current:
        log(f"{OVERRIDE_FILE} sets no core settings")
    for service, env in current.items():
        for key, value in env.items():
            log(f"{service}: {key}" + ("" if value is None else f"={value}"))


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
        name = path.relative_to(ctx.repo)
        diff = difflib.unified_diff(a, b, f"{name} (current)", f"{name} (rendered)", lineterm="")
        for line in diff:
            print(line)


def _rotate(args: argparse.Namespace, ctx: Ctx) -> None:
    match args.what:
        case "db-password":
            if not args.which:
                raise workflow.Blocked("rotate db-password needs --which " + "|".join(databases.BY_KEY))
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
            token.rotate_engine_token(ctx)
        case "keycloak-client":
            rotate.keycloak_client(ctx)


if __name__ == "__main__":
    sys.exit(main())
