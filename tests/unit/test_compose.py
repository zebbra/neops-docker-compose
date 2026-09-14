import pytest

from neops_compose.compose import Compose, ComposeError


class FakeRun:
    """Records every subprocess.run call and replays a canned docker compose ps."""

    PS_JSON = (
        '{"Service":"cms","State":"running","Health":"healthy"}\n'
        '{"Service":"web","State":"exited","Health":""}\n'
    )

    def __init__(self, returncode: int = 0, stderr: str = ""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        outer = self

        class R:
            returncode = outer.returncode
            stdout = outer.PS_JSON
            stderr = outer.stderr

        return R()


def test_compose_runs_docker_compose_from_the_repo_root(tmp_path, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr("neops_compose.compose.subprocess.run", fake)
    c = Compose(tmp_path)
    ps = c.ps()
    assert fake.calls[0][0][:3] == ["docker", "compose", "ps"] and fake.calls[0][1]["cwd"] == tmp_path
    assert ps == [
        {"Service": "cms", "State": "running", "Health": "healthy"},
        {"Service": "web", "State": "exited", "Health": ""},
    ]
    assert c.running_services() == {"cms"}
    c.up("cms", "engine", force_recreate=True)
    assert fake.calls[-1][0] == [
        "docker",
        "compose",
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "900",
        "--force-recreate",
        "cms",
        "engine",
    ]


def test_exec_keeps_env_values_out_of_argv(tmp_path, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr("neops_compose.compose.subprocess.run", fake)
    Compose(tmp_path).exec("cms", "python", "manage.py", "check", env={"X": "s3cret"})
    args, kwargs = fake.calls[-1]
    assert args == ["docker", "compose", "exec", "-T", "-e", "X", "cms", "python", "manage.py", "check"]
    assert "s3cret" not in " ".join(args)
    assert kwargs["env"]["X"] == "s3cret"
    assert kwargs["env"]["PATH"], "the caller's environment must be inherited, not replaced"


def test_run_without_env_leaves_the_environment_alone(tmp_path, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr("neops_compose.compose.subprocess.run", fake)
    Compose(tmp_path).run("ps")
    assert fake.calls[-1][1]["env"] is None


def test_failure_raises_without_echoing_env_values(tmp_path, monkeypatch):
    fake = FakeRun(returncode=1, stderr="permission denied")
    monkeypatch.setattr("neops_compose.compose.subprocess.run", fake)
    with pytest.raises(ComposeError) as excinfo:
        Compose(tmp_path).exec("cms", "psql", env={"PGPASSWORD": "s3cret"})
    message = str(excinfo.value)
    assert "PGPASSWORD" in message and "s3cret" not in message
    assert "permission denied" in message
