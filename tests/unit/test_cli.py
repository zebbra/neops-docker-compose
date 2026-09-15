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
