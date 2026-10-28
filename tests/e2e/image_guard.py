#!/usr/bin/env python3
"""Prove a run used the images it was meant to: record image ids before, compare after.

    uv run python tests/e2e/image_guard.py record <deployment dir> <ids.json> [--ref IMAGE ...]
    uv run python tests/e2e/image_guard.py verify <deployment dir> <ids.json>

`up` pulls with --ignore-pull-failures, so a local retag under a published name is silently
replaced the moment that name is published. `record` stores the id each reference resolves to
(by default every image of the deployment's compose config); `verify` requires every reference
to resolve to the same id and every container started from one of them to run exactly it.
Exit code 0 only when nothing moved; a run whose guard fails is void.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from neops_compose.compose import Compose  # noqa: E402

MISSING = "<missing>"


def image_id(reference: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Id}}", reference], capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else MISSING


def image_ids(references: list[str]) -> dict[str, str]:
    return {ref: image_id(ref) for ref in references}


def container_image(name: str) -> str:
    result = subprocess.run(["docker", "inspect", "-f", "{{.Image}}", name], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else MISSING


def project_containers(base: Path) -> dict[str, tuple[str, str]]:
    """Container name -> (the reference it was started from, the image id it runs)."""
    return {row["Name"]: (row.get("Image", ""), container_image(row["Name"])) for row in Compose(base).ps()}


def guard_problems(
    before: dict[str, str], after: dict[str, str], containers: dict[str, tuple[str, str]]
) -> list[str]:
    problems = [
        f"{ref} resolved to {before[ref]} and now to {after.get(ref, MISSING)}"
        for ref in sorted(before)
        if before[ref] == MISSING or after.get(ref) != before[ref]
    ]
    for name, (ref, running) in sorted(containers.items()):
        if ref in before and running != before[ref]:
            problems.append(f"{name} runs {running}, not the recorded {ref} ({before[ref]})")
    return problems


def record(base: Path, out: Path, refs: list[str]) -> int:
    ids = image_ids(refs or Compose(base).images())
    out.write_text(json.dumps(ids, indent=2, sort_keys=True) + "\n")
    for ref, value in sorted(ids.items()):
        print(f"{ref} {value}")
    return 1 if MISSING in ids.values() else 0


def verify(base: Path, recorded: Path) -> int:
    before = json.loads(recorded.read_text())
    problems = guard_problems(before, image_ids(sorted(before)), project_containers(base))
    for problem in problems:
        print(f"FAIL {problem}")
    print("image guard " + ("void: images moved" if problems else f"clean: {len(before)} references held"))
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("record", "verify"))
    ap.add_argument("base", type=Path, help="the deployment directory")
    ap.add_argument("ids", type=Path, help="where record writes the ids and verify reads them")
    ap.add_argument(
        "--ref", action="append", default=[], help="record only this image reference (repeatable)"
    )
    args = ap.parse_args(argv)
    if args.action == "record":
        return record(args.base.resolve(), args.ids, args.ref)
    return verify(args.base.resolve(), args.ids)


if __name__ == "__main__":
    sys.exit(main())
