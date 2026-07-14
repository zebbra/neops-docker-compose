# neops GitOps demo — multi-instance git sync

Minimal neops stacks (backend, worker, beat, legacy frontend, redis,
postgres, elasticsearch) to exercise the `neops_git_sync` workflow with
multiple instances bound to different branches of one task repository.

**One compose file, four instances.** Each env file in `envs/` sets the
compose project name, host ports and the git-sync role — compose project
isolation gives every instance its own containers, network and volumes:

| Instance | Backend | Frontend | Branch | Mode |
|---|---|---|---|---|
| dev-main | :8001 | :8081 | `main` | bidirectional |
| dev-prod | :8002 | :8082 | `prod` | bidirectional |
| cust-dev | :8003 | :8083 | `customer-dev` | bidirectional |
| cust-prod | :8004 | :8084 | `customer-prod` | **pull-only** (git is SoT) |

Ports 8000/8080 stay free for a natively running dev setup.

## Prerequisites

- Local checkouts of `neops-core` and `neops-legacy-frontend` as siblings
  of this repo (override with `NEOPS_CORE_PATH` / `NEOPS_LEGACY_FRONTEND_PATH`).
- A git repository for the task definitions with the four branches created
  (empty branches are fine — each instance can run the initial push).
- RAM: each instance runs its own elasticsearch (capped at 512M heap) —
  budget roughly 2–3 GB per instance. Run only the instances you need.

## Quick start

```bash
# 1. Build the shared images once (both instances reuse the same tags)
make build

# 2. Start an instance
make up INSTANCE=cust-prod
make up INSTANCE=cust-dev

# 3. Watch it come up (init runs migrations + elastic setup on first start)
make logs INSTANCE=cust-prod
```

Frontend: http://localhost:8084 — backend GraphQL: http://localhost:8004/graphql

## Per-instance setup (once, after first start)

```bash
# Create an admin user
docker compose --env-file envs/cust-prod.env exec backend ./manage.py createsuperuser

# Bootstrap permissions (Administrators role on all scopes)
docker compose --env-file envs/cust-prod.env exec backend ./manage.py neops_permissions_setup --user <username>
```

Then configure the git credentials in the Django admin
(`http://localhost:8004/admin` → *Django 3Party Credentials*, app key
`neops_git_sync`): url = repo URL, username + password = HTTPS token, or
key = SSH private key.

## Exercising the workflow

```bash
# Initial push from the instance that owns the content (e.g. cust-dev)
make manage INSTANCE=cust-dev CMD="neops_git_sync push-all"

# Pull on another instance (or wait for its scheduled pull / webhook)
make manage INSTANCE=cust-prod CMD="neops_git_sync pull"

# Inspect sync state
make manage INSTANCE=cust-prod CMD="neops_git_sync status"
```

Promotion flow to demo: edit a task in cust-dev (auto-pushes to
`customer-dev`) → merge `customer-dev` → `customer-prod` on the git host →
pull on cust-prod (pull-only, `NEOPS_GIT_SYNC_AUTO_PUSH=false`) → the task
appears/updates there. The Git Sync panel on the Tools page
(`/scopes/global/configuration/ide`) shows branch, last sync and the
commit log per instance.

For periodic pulls create a NeopsTask with provider `enterprise-git-sync`
(direction: pull) and schedule it via neops_cron, or trigger it through
neops_webhook from the git host. See the module README in
`neops-core/backend/neops_modules/enterprise/neops_git_sync/` for the
full trigger matrix (webhook / cron / pre-run).

## Notes

- All state lives in named volumes per instance; `make down` keeps them,
  `docker compose --env-file envs/<i>.env down -v` wipes an instance.
- The four env files are the only per-instance configuration — add more
  instances by adding another env file with unique project name + ports.
- Images are demo-tagged (`neops-enterprise:gitops-demo`); rerun
  `make build` after changing backend or frontend code.
