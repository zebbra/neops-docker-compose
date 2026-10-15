import argparse
import ast
import importlib
import inspect
import pkgutil
import subprocess
import sys

import pytest

import neops_compose
from neops_compose import cli
from neops_compose.cli import build_parser


def registered_commands() -> set[str]:
    return set(build_parser()._subparsers._group_actions[0].choices)


def dispatch_match() -> ast.Match:
    tree = ast.parse(inspect.getsource(cli.dispatch))
    return next(node for node in ast.walk(tree) if isinstance(node, ast.Match))


def test_every_command_is_wired():
    subs = registered_commands()
    assert subs >= {
        "install",
        "up",
        "down",
        "ps",
        "logs",
        "restart",
        "compose",
        "check",
        "migrate",
        "secrets",
        "keys",
        "token",
        "render",
        "doctor",
        "status",
        "backup",
        "rotate",
        "purge",
        "version",
    }


def test_dispatch_handles_every_command_the_parser_accepts():
    """`version` is answered in main() before a Ctx is built, because it must work without
    a .env; every other subcommand has to reach a case here or it silently exits 0."""
    handled = {
        case.pattern.value.value
        for case in dispatch_match().cases
        if isinstance(case.pattern, ast.MatchValue) and isinstance(case.pattern.value, ast.Constant)
    }
    assert handled == registered_commands() - {"version"}


def test_an_unhandled_command_is_loud():
    """Without the wildcard, a subcommand added to the parser and forgotten here returns 0
    having done nothing at all."""
    wildcard = [case for case in dispatch_match().cases if isinstance(case.pattern, ast.MatchAs)]
    assert wildcard and wildcard[0].pattern.pattern is None
    with pytest.raises(SystemExit, match="unhandled command invented"):
        cli.dispatch(argparse.Namespace(command="invented"), argparse.Namespace(compose=None))


def package_exceptions() -> set[type]:
    found = set()
    for module in pkgutil.iter_modules(neops_compose.__path__):
        namespace = importlib.import_module(f"neops_compose.{module.name}")
        found.update(
            obj
            for obj in vars(namespace).values()
            if isinstance(obj, type)
            and issubclass(obj, Exception)
            and obj.__module__.startswith("neops_compose.")
        )
    return found


def test_every_exception_this_package_defines_is_reported_as_an_error():
    """These all name an operator error or a state their deployment is in. One missing from
    ERRORS reaches the terminal as a traceback, which reads as a bug in the CLI."""
    assert package_exceptions() == set(cli.ERRORS)


def test_module_runs():
    out = subprocess.run(
        [sys.executable, "-m", "neops_compose.cli", "version"], capture_output=True, text=True
    )
    assert out.returncode == 0 and out.stdout.strip() == "2.0.0"


def test_render_diff_names_nested_files_by_their_repo_relative_path(tmp_repo, capsys):
    """Two generated files can share a basename, so the label has to carry the directory."""
    from neops_compose.cli import _render_diff
    from neops_compose.context import Ctx
    from neops_compose.env import Env
    from neops_compose.paths import Paths
    from neops_compose.scenario import Scenario

    (tmp_repo / ".env").write_text(
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml\n"
        "NEOPS_WEB_URL=https://neops.example.com\n"
        "NEOPS_CMS_URL=https://cms.neops.example.com\n"
        "NEOPS_ENGINE_URL=https://engine.neops.example.com\n"
        "NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com\n"
    )
    env = Env(tmp_repo / ".env")
    paths = Paths.for_repo(tmp_repo, env)
    ctx = Ctx(tmp_repo, env, paths, Scenario.from_env(env), None, None, print)
    _render_diff(ctx)
    out = capsys.readouterr().out
    assert "generated/traefik/dynamic.yml (rendered)" in out
    assert "generated/dynamic.yml" not in out
