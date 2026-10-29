from pathlib import Path

from neops_compose.env import Env
from neops_compose.rules import ALL_SECRET_KEYS, problems, warnings
from neops_compose.scenario import Scenario

REPO = Path(__file__).resolve().parents[2]


def filled(example: Path, tmp_path: Path) -> Env:
    """An example with every secret filled with a dummy value, as an operator would."""
    text = example.read_text()
    for key in ALL_SECRET_KEYS:
        text = text.replace(f"\n{key}=\n", f"\n{key}=dummy{key.lower()}0123456789\n")
    target = tmp_path / ".env"
    target.write_text(text)
    return Env(target)


def test_every_example_validates_once_secrets_are_filled(tmp_path):
    examples = sorted((REPO / "examples").glob("*.env"))
    assert len(examples) == 16
    for example in examples:
        env = filled(example, tmp_path)
        assert problems(env, Scenario.from_env(env), REPO) == [], example.name
        assert warnings(env, Scenario.from_env(env), REPO) == [], example.name


def test_the_logs_examples_select_grafana_before_the_overlays_that_need_it():
    for name in ("metrics.env", "logs.env", "metrics-logs.env"):
        files = Scenario.from_env(Env(REPO / "examples" / name)).files
        assert "compose.grafana.yaml" in files, name
        grafana_at = files.index("compose.grafana.yaml")
        dependants = [f for f in files if f in ("compose.metrics.yaml", "compose.logs.yaml")]
        assert dependants and all(grafana_at < files.index(f) for f in dependants), name


def test_examples_ship_no_secrets_unless_they_declare_themselves_insecure():
    """A secret spelled out in an example is a known secret the moment someone copies the file,
    which is why they ship blank. The single exception is an example that also turns the
    strength rules off: examples/local.env ships `neops:neops` on purpose, and saying so with
    NEOPS_DISABLE_SECURITY_CHECKS is what keeps that exception from spreading unnoticed."""
    for example in [REPO / ".env.example", *(REPO / "examples").glob("*.env")]:
        if Env(example).flag("NEOPS_DISABLE_SECURITY_CHECKS"):
            continue
        for line in example.read_text().splitlines():
            for key in ALL_SECRET_KEYS:
                if line.startswith(key + "="):
                    assert line == key + "=", f"{example.name}: {key} must ship empty"
