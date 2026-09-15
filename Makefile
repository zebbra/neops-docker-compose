include .make_scripts/release-management/release-management-makefile
# This includes make: tag-major, tag-major-beta, tag-minor, tag-minor-beta, tag-patch, tag-patch-beta, tag-latest-beta, tag-major-minor-ruleset, hard-reset-tags, check-for-release and sync-release-assets.
include .make_scripts/mkdocs-documentation/mkdocs-documentation-makefile.mk
.PHONY: help check lint format test compose-config e2e

help:
	@echo "check          lint + unit tests + compose config gate for every example"
	@echo "lint           ruff check + format check"
	@echo "format         ruff format"
	@echo "test           unit tests"
	@echo "compose-config docker compose config for every examples/*.env"
	@echo "e2e            end-to-end run of one scenario: make e2e SCENARIO=traefik-tls-selfsigned"
	@echo "Operate a deployment with ./neops (see README.md)."

check: lint test compose-config

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

test:
	uv run pytest -q

compose-config:
	uv run python tests/compose_config_check.py

e2e:
	@test -n "$(SCENARIO)" || { echo "usage: make e2e SCENARIO=<examples name without .env>"; exit 1; }
	uv run python tests/e2e/run_scenario.py $(SCENARIO)
