from neops_compose.compose import Compose


def test_compose_runs_docker_compose_from_the_repo_root(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))

        class R:
            returncode = 0
            stdout = (
                '{"Service":"cms","State":"running","Health":"healthy"}\n'
                '{"Service":"web","State":"exited","Health":""}\n'
            )
            stderr = ""

        return R()

    monkeypatch.setattr("neops_compose.compose.subprocess.run", fake_run)
    c = Compose(tmp_path)
    ps = c.ps()
    assert calls[0][0][:3] == ["docker", "compose", "ps"] and calls[0][1]["cwd"] == tmp_path
    assert ps == [
        {"Service": "cms", "State": "running", "Health": "healthy"},
        {"Service": "web", "State": "exited", "Health": ""},
    ]
    assert c.running_services() == {"cms"}
    c.up("cms", "engine", force_recreate=True)
    assert calls[-1][0] == [
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
    c.exec("cms", "python", "manage.py", "check", env={"X": "1"})
    assert calls[-1][0][:8] == ["docker", "compose", "exec", "-T", "-e", "X=1", "cms", "python"]
