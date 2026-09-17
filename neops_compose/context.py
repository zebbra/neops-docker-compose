from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from neops_compose.compose import Compose
from neops_compose.env import Env
from neops_compose.paths import Paths
from neops_compose.scenario import Scenario
from neops_compose.state import State

# compose.yaml's ${NEOPS_ADMIN_USER:-neops} and .env.example document the same value.
DEFAULT_ADMIN_USER = "neops"


@dataclass
class Ctx:
    """One deployment: its .env, where its bytes live, what compose it drives, what the CLI did to it."""

    repo: Path
    env: Env
    paths: Paths
    scenario: Scenario
    compose: Compose
    state: State
    log: Callable[[str], None]

    @classmethod
    def build(cls, repo: Path, log: Callable[[str], None]) -> Ctx:
        env = Env(repo / ".env")
        paths = Paths.for_repo(repo, env)
        return cls(repo, env, paths, Scenario.from_env(env), Compose(repo), State.load(paths.state_file), log)

    def save_state(self) -> None:
        self.state.save(self.paths.state_file)
