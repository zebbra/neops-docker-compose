# neops GitOps demo — multi-instance git sync

Minimal neops stacks (backend, worker, beat, legacy frontend, redis,
postgres, elasticsearch) to exercise the `neops_git_sync` workflow with
multiple instances bound to different branches of one task repository.

`neops_netbox` is enabled by default (the demo repo's tasks use its
providers, e.g. `generic-netbox-jinja-facts`) so pulled tasks validate
and import correctly. Pulling/importing works without it; actually
*running* a netbox-provider task additionally needs a NetBox connection
configured for that instance — out of scope for this demo.

**One compose file, four instances.** Each env file in `envs/` sets the
compose project name, host ports and the git-sync role — compose project
isolation gives every instance its own containers, network and volumes:

Two branches: `dev` (working branch) and `prod` (released state) — create both
on the git host before syncing. Local engineer instances and the customer dev
instance share `dev`; promotion to `prod` happens via merge on the git host.

| Instance | Backend | Frontend | Branch | Mode |
|---|---|---|---|---|
| dev-main (local dev) | :8001 | :8081 | `dev` | bidirectional |
| dev-prod (local prod) | :8002 | :8082 | `prod` | bidirectional |
| cust-dev | :8003 | :8083 | `dev` | bidirectional |
| cust-prod | :8004 | :8084 | `prod` | **pull-only** (git is SoT) |

> Note: dev-main and cust-dev are BOTH auto-push writers on `dev` — that is
> deliberate for the demo (it exercises the concurrent-edit behaviour), but
> for a real deployment keep one bidirectional instance per branch.

Ports 8000/8080 stay free for a natively running dev setup.

## Prerequisites

- Local checkouts of `neops-core` and `neops-legacy-frontend` as siblings
  of this repo (override with `NEOPS_CORE_PATH` / `NEOPS_LEGACY_FRONTEND_PATH`).
- A git repository for the task definitions with the `dev` and `prod`
  branches created (empty branches are fine — each instance can run the
  initial push). A read-write PAT/SSH key for instances that auto-push.
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

Then configure the git credentials (public repo: url alone is enough to pull):

```bash
make manage INSTANCE=cust-prod CMD="save_credentials neops_git_sync --url https://github.com/zebbra/neops-git-demo --username bot --password ghp_xxx"
# verify (secrets masked)
make manage INSTANCE=cust-prod CMD="save_credentials neops_git_sync"
```

Alternatively via the Django admin (`http://localhost:8004/admin` →
*Django 3Party Credentials*, app key `neops_git_sync`).

## Exercising the workflow

```bash
# Initial push from the instance that owns the content (e.g. cust-dev)
make manage INSTANCE=cust-dev CMD="neops_git_sync push-all"

# Pull on another instance (or wait for its scheduled pull / webhook)
make manage INSTANCE=cust-prod CMD="neops_git_sync pull"

# Inspect sync state
make manage INSTANCE=cust-prod CMD="neops_git_sync status"
```

Promotion flow to demo: edit a task in cust-dev (auto-pushes to `dev`) →
merge `dev` → `prod` on the git host → pull on cust-prod (pull-only,
`NEOPS_GIT_SYNC_AUTO_PUSH=false`) → the task appears/updates there. Every
instance also has a built-in periodic pull registered via beat (every
`NEOPS_GIT_SYNC_PULL_INTERVAL_MINUTES`, default 5 — cheap thanks to the
`ls-remote` fast path), so cust-prod picks up the merge on its own within
that window even without a manual pull.

The dedicated **Git Sync page** (top-level nav entry, or
`/scopes/global/git`) shows branch, mode, repository link, last sync,
snapshot age, working tree state, any sync error, and the full commit
log per instance. Task list/edit/create pages also raise a snackbar the
moment a sync starts failing.

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
