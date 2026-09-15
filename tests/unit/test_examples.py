from pathlib import Path

from neops_compose.env import Env
from neops_compose.rules import ALL_SECRET_KEYS, problems
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
    assert len(examples) == 9
    for example in examples:
        env = filled(example, tmp_path)
        assert problems(env, Scenario.from_env(env), REPO) == [], example.name


def test_examples_ship_no_secrets():
    for example in [REPO / ".env.example", *(REPO / "examples").glob("*.env")]:
        for line in example.read_text().splitlines():
            for key in ALL_SECRET_KEYS:
                if line.startswith(key + "="):
                    assert line == key + "=", f"{example.name}: {key} must ship empty"
