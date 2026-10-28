"""The image-id guard's verdict, checked on recorded ids without Docker."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import image_guard  # noqa: E402

ENGINE = "quay.io/zebbra/neops-workflow-engine:0.44.0-beta.1"
CORE = "quay.io/zebbra/neops-core:2.3.0-beta.1"
BEFORE = {ENGINE: "sha256:e1", CORE: "sha256:c1"}


def test_unchanged_ids_and_matching_containers_are_clean():
    containers = {"p-engine-1": (ENGINE, "sha256:e1"), "p-redis-1": ("redis:7-alpine", "sha256:r")}
    assert image_guard.guard_problems(BEFORE, dict(BEFORE), containers) == []


def test_a_reference_that_now_resolves_elsewhere_voids_the_run():
    after = BEFORE | {CORE: "sha256:pulled"}
    assert image_guard.guard_problems(BEFORE, after, {}) == [
        f"{CORE} resolved to sha256:c1 and now to sha256:pulled"
    ]


def test_a_container_started_from_another_image_voids_the_run():
    problems = image_guard.guard_problems(BEFORE, dict(BEFORE), {"p-engine-1": (ENGINE, "sha256:e0")})
    assert problems == [f"p-engine-1 runs sha256:e0, not the recorded {ENGINE} (sha256:e1)"]


def test_a_reference_missing_before_the_run_is_never_clean():
    before = {ENGINE: image_guard.MISSING}
    assert image_guard.guard_problems(before, dict(before), {}) != []
