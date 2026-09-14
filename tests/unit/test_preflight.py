from neops_compose import preflight


def test_compose_version_parsing():
    assert preflight.version_ok("v2.24.1", (2, 24)) is True
    assert preflight.version_ok("2.20.3", (2, 24)) is False
    assert preflight.version_ok("5.4.0", (2, 24)) is True


def test_port_free_detects_a_bound_port():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert preflight.port_free("127.0.0.1", port) is False
    finally:
        s.close()
    assert preflight.port_free("127.0.0.1", port) is True


def test_required_ports_by_scenario(tmp_path):
    from neops_compose.env import Env
    from neops_compose.scenario import Scenario

    (tmp_path / ".env").write_text(
        "COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.traefik-shared-host.yaml:"
        "compose.tls-files.yaml\nNEOPS_HTTP_PORT=8880\n"
    )
    env = Env(tmp_path / ".env")
    assert preflight.required_ports(env, Scenario.from_env(env)) == [
        ("0.0.0.0", 8880),
        ("0.0.0.0", 443),
        ("0.0.0.0", 8443),
    ]
    (tmp_path / ".env").write_text("COMPOSE_FILE=compose.yaml:compose.expose.yaml\nNEOPS_CMS_PORT=9000\n")
    env = Env(tmp_path / ".env")
    assert preflight.required_ports(env, Scenario.from_env(env)) == [
        ("127.0.0.1", 8080),
        ("127.0.0.1", 9000),
        ("127.0.0.1", 3030),
        ("127.0.0.1", 3031),
    ]


def test_report_format():
    checks = [preflight.Check("docker", True, "29.7.2"), preflight.Check("ports", False, "443 is in use")]
    text = preflight.format_report(checks)
    assert "OK   docker" in text and "FAIL ports" in text and "443 is in use" in text
    assert preflight.all_ok(checks) is False
