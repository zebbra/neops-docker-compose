import subprocess
import sys

from neops_compose.cli import build_parser


def test_every_command_is_wired():
    parser = build_parser()
    subs = parser._subparsers._group_actions[0].choices
    assert set(subs) >= {
        "install",
        "up",
        "down",
        "ps",
        "logs",
        "restart",
        "compose",
        "check",
        "migrate",
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
