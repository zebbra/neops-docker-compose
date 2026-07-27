INSTANCE ?= dev-main
INSTANCES = dev-main dev-prod cust-dev cust-prod
COMPOSE = docker compose --env-file envs/$(INSTANCE).env

NEOPS_CORE_PATH ?= ../../neops-core
NEOPS_LEGACY_FRONTEND_PATH ?= ../../neops-legacy-frontend

.PHONY: build up down logs ps shell manage up-all down-all ps-all help

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'
	@echo ""
	@echo "  INSTANCE=$(INSTANCE)  (one of: $(INSTANCES))"

build:  ## Build the shared backend + frontend images from the local checkouts
	docker build $(NEOPS_CORE_PATH) --target enterprise -t neops-enterprise:gitops-demo
	docker build $(NEOPS_LEGACY_FRONTEND_PATH) -t neops-legacy-frontend:gitops-demo

up:  ## Start one instance: make up INSTANCE=cust-prod
	$(COMPOSE) up -d

down:  ## Stop one instance (volumes are kept)
	$(COMPOSE) down

logs:  ## Tail logs of one instance
	$(COMPOSE) logs -f --tail 100

ps:  ## Show containers of one instance
	$(COMPOSE) ps

shell:  ## Django shell in the backend of one instance
	$(COMPOSE) exec backend ./manage.py shell

manage:  ## Run a management command: make manage INSTANCE=cust-prod CMD="neops_git_sync status"
	$(COMPOSE) exec backend ./manage.py $(CMD)

up-all:  ## Start all four instances
	@for i in $(INSTANCES); do $(MAKE) --no-print-directory up INSTANCE=$$i; done

down-all:  ## Stop all four instances
	@for i in $(INSTANCES); do $(MAKE) --no-print-directory down INSTANCE=$$i; done

ps-all:  ## Show containers of all instances
	@for i in $(INSTANCES); do echo "== $$i"; $(MAKE) --no-print-directory ps INSTANCE=$$i; done
