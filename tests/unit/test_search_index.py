import subprocess

import pytest

from neops_compose import search_index
from neops_compose.compose import DOCKER_MISSING, ComposeError

TOP_HEADER = "UID    PID    PPID   C    STIME   TTY   TIME       CMD\n"
TOP_SERVING = (
    "neops-cms-1\n" + TOP_HEADER + "root   4127   1      0    09:12   ?     00:00:09   gunicorn neops.wsgi\n"
)
TOP_REBUILDING = TOP_SERVING + (
    "root  5310  5288  97  09:40  ?  00:03:41  python -u manage.py elastic_index --rebuild-if-stale\n"
)


class TopCompose:
    def __init__(self, running=("cms",), stdout="", returncode=0, raises=None):
        self.running = set(running)
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.runs: list[tuple[tuple[str, ...], bool, bool]] = []

    def running_services(self):
        return self.running

    def run(self, *args, capture=False, check=True, env=None):
        self.runs.append((args, capture, check))
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(args, self.returncode, stdout=self.stdout, stderr="")


TOP_OLD_REBUILD = TOP_SERVING + (
    "root  5310  5288  97  09:40  ?  00:03:41  python -u manage.py elastic_index --rebuild --force\n"
)


class Ctx:
    def __init__(self, compose):
        self.compose = compose
        self.logged: list[str] = []
        self.log = self.logged.append


def notices(compose) -> list[str]:
    ctx = Ctx(compose)
    search_index.warn_if_rebuild_running(ctx)
    return ctx.logged


def test_a_stopped_cms_is_not_asked_for_its_processes():
    compose = TopCompose(running=("postgres",))
    assert notices(compose) == []
    assert compose.runs == []


def test_a_rebuild_left_behind_in_cms_says_up_will_wait_for_it():
    compose = TopCompose(stdout=TOP_REBUILDING)
    (line,) = notices(compose)
    assert compose.runs == [(("top", "cms"), True, False)]
    assert line.startswith(search_index.LEFT_RUNNING)
    assert "up will wait for it. To abort it: ./neops restart cms" in line
    assert line.endswith(search_index.SEE_DOCS)


def test_an_older_cores_rebuild_left_behind_is_not_waited_for():
    """Only --rebuild-if-stale takes core's rebuild lock."""
    (line,) = notices(TopCompose(stdout=TOP_OLD_REBUILD))
    assert "nothing waits for it" in line and "./neops restart cms" in line
    assert "will wait" not in line


def test_a_cms_that_only_serves_runs_no_rebuild():
    assert notices(TopCompose(stdout=TOP_SERVING)) == []


@pytest.mark.parametrize(
    "compose",
    [
        TopCompose(stdout=TOP_REBUILDING, returncode=1),
        TopCompose(raises=ComposeError(DOCKER_MISSING)),
        TopCompose(stdout=None),
    ],
)
def test_a_process_table_that_cannot_be_read_never_stops_the_start(compose):
    assert notices(compose) == []
