"""Parsing what a probe run inside a stack container prints, checked without a container."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))

import inside  # noqa: E402


def test_the_status_line_and_the_body_are_split():
    assert inside.parse_status('503\n{"code":"X"}\n') == (503, '{"code":"X"}')


def test_output_without_a_status_line_is_kept_as_the_reason():
    assert inside.parse_status("error connect ECONNREFUSED\n") == (None, "error connect ECONNREFUSED")
