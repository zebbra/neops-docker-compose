from __future__ import annotations

import secrets as pysecrets

from neops_compose.env import Env
from neops_compose.rules import generated_secrets
from neops_compose.scenario import Scenario

# Typed into a login form by a person, so shorter than the machine-only ones.
TYPED_BY_HUMANS = frozenset(
    {"NEOPS_ADMIN_PASSWORD", "NEOPS_KEYCLOAK_ADMIN_PASSWORD", "NEOPS_GRAFANA_ADMIN_PASSWORD"}
)


def fill_missing(env: Env, scenario: Scenario) -> list[str]:
    """Set every generated secret the scenario needs that is blank or absent in .env.

    Anything already set is kept, placeholders included: `check` names those, and replacing a
    value an operator typed is not this command's call.
    """
    filled = []
    for key in generated_secrets(scenario):
        if env.is_set(key):
            continue
        env.set(key, _new_value(key))
        filled.append(key)
    return filled


def _new_value(key: str) -> str:
    return pysecrets.token_hex(12 if key in TYPED_BY_HUMANS else 32)
