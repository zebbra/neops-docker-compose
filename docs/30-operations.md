---
title: Operations
description: Day-2 commands, backup and restore, secret rotation, migrations, upgrades and purge.
tags: [howto, reference]
---

# Operations

Every command below runs from the repo root, against the `.env` in place there. There is no
`--env` flag, so a host running more than one deployment needs one checkout per deployment.

## Commands

| Command | What it does |
|---|---|
| `./neops up [--allow-downgrade]` | migrate, render, pull, start every service, doctor. Run this after every `git pull` or `.env` edit. |
| `./neops down` | stop and remove every container (`docker compose down --remove-orphans`); data is kept |
| `./neops ps` | container status |
| `./neops logs [service ...]` | follow logs, no services means all |
| `./neops restart <service ...>` | restart specific services |
| `./neops compose -- <args>` | pass-through to `docker compose` with this deployment's `COMPOSE_FILE`; `compose -- config` prints every secret, so treat its output accordingly |
| `./neops check` | preflight only, no changes |
| `./neops migrate [--dry-run] [--fake NAME]` | apply pending deployment migrations |
| `./neops keys` | generate any missing key material; never overwrites |
| `./neops token` | mint the engine's CMS API key if missing or invalid |
| `./neops render [--diff]` | rebuild `generated/` from `.env`; `--diff` shows what would change without writing |
| `./neops doctor [--connect ADDR] [--insecure] [--probe-ratelimit]` | health report through the public URLs |
| `./neops status` | pinned vs. running image tags, pending migrations, last successful `up` |
| `./neops backup [--dir DIR] [--keep N]` | logical backup, see below |
| `./neops rotate <what> [...]` | secret rotation, see the matrix below |
| `./neops purge --confirm <data dir>` | irreversibly delete the installation |

## Backup

```bash
./neops backup --keep 14
```

Writes `backups/<UTC timestamp>/`, mode 0700 with every file inside 0600:

- `cms.dump`, `engine.dump`, `keycloak.dump`: a `pg_dump -Fc` of each database whose Postgres
  container is currently running (the keycloak dump only when that overlay is in use);
- `.env`, a copy of `data/secrets/`, `certs/`, and `cust-cert/`;
- `manifest.json`: the CLI version, every pinned image, applied and faked migrations, and which
  compose files made up the scenario at backup time.

**A backup archive is a credential.** It contains the JWT signing key, the engine's CMS API key,
the Keycloak client secret, and (via `.env`) every database and admin password. Store it the way
you would store those secrets directly.

`--keep N` prunes older archives after writing the new one, keeping the newest `N`. Elasticsearch
is never included: it holds derived data, rebuilt after a restore (see below).

A plain file copy of `data/` is a valid backup only while every container is stopped; while the
stack is running, `./neops backup`'s logical dumps are the only sanctioned way to get a consistent
copy.

## Restore

There is no `./neops restore`; restoring is a manual sequence, deliberately, because it needs
judgment about which secrets to keep:

```bash
# 0. On a fresh host: check this repo out at the release the backup names in its manifest.json,
#    and put the archive where the commands below can read it.
git clone <this repo> neops && cd neops
mkdir -p backups && cp -a /media/<the archive>/<timestamp> backups/

# 1. Put the deployment's .env back (or reconcile it with the current one) and prepare data/.
cp backups/<timestamp>/.env .env
./neops migrate                                    # creates data/ if this is a fresh host
cp -a backups/<timestamp>/secrets/. data/secrets/
cp -a backups/<timestamp>/certs/. certs/            # if the scenario uses certs/
cp -a backups/<timestamp>/cust-cert/. cust-cert/    # if you use custom CAs

# 2. Start only the databases and load the dumps. --wait matters: pg_restore cannot connect
#    while Postgres is still initialising, and `up -d` returns before it is.
./neops render
./neops compose -- up -d --wait postgres-cms postgres-engine   # + postgres-keycloak, if used
./neops compose -- exec -T postgres-cms pg_restore -U neops -d neops --clean --if-exists \
  < backups/<timestamp>/cms.dump
./neops compose -- exec -T postgres-engine pg_restore -U postgres -d neops-workflow --clean --if-exists \
  < backups/<timestamp>/engine.dump
# keycloak, if used:
./neops compose -- exec -T postgres-keycloak pg_restore -U keycloak -d keycloak --clean --if-exists \
  < backups/<timestamp>/keycloak.dump

# 3. Bring up the rest and rebuild the search index (not part of the backup).
./neops up
./neops compose -- exec cms python manage.py elastic_index --populate --models core.Device core.Interface

# 4. Confirm.
./neops doctor
```

`--if-exists` is not optional on a fresh database: without it every `DROP` in the dump fails
against a schema that does not exist yet, and `pg_restore` ends with hundreds of errors and a
non-zero exit while having loaded the data correctly — indistinguishable from a real failure.

The indices themselves need no `elastic_index --create`: `up` runs `cms-init`, which creates
them. Only the contents have to be rebuilt, which is what `--populate` does. Running `--create`
after `up` fails with `resource_already_exists_exception`.

Restoring onto a host whose `neops-core` image is *older* than the one the backup was taken with
is not supported: Django migrations do not run backwards. Restore onto the same or a newer core
version.

## Rotating secrets

`./neops rotate <what>` is the only sanctioned way to change a secret once installed. Editing a
password directly in `.env` desyncs it from the running database, and `./neops check` detects and
names that mismatch on the next run rather than letting it fail obscurely later.

| `what` | Effect | What it invalidates | Restarts |
|---|---|---|---|
| `db-password --which cms\|engine\|keycloak` | `ALTER ROLE` in that Postgres container with a fresh random password, then updates `.env` | nothing session-visible | the services that connect to that database |
| `admin-password [--password P]` | sets a new password for `NEOPS_ADMIN_USER` via `manage.py`, then updates `.env` | the previous password | none |
| `secret-key` | rotates `DJANGO_SECRET_KEY` | **every session and every static API key** (they are signed with it, including the engine's own token) | all four core services, then re-mints the engine's token automatically |
| `jwt` | replaces the RSA keypair the CMS signs and the engine verifies with | **every user session** | `cms`, `cms-worker`, `cms-beat`, `engine` |
| `tls` | re-issues the self-signed certificate for the hostnames currently in `.env` | nothing; refuses if you are not using `NEOPS_TLS_SELF_SIGNED=true` (replace files under `certs/` by hand otherwise) | `traefik` |
| `token` | mints a new engine CMS API key and revokes the previous one (best-effort; a failed revoke is logged, not fatal) | the previous engine token | `engine` |
| `keycloak-client` | generates a new client secret, sets it on the `neops-auth` client in Keycloak, re-renders `generated/providers.json`, and re-seeds the CMS's OIDC provider config | the previous client secret | none (config is reloaded, not restarted) |

`secret-key` and `jwt` are the two that end every active login. Plan them like a maintenance
window.

No rotation puts a secret on a command line of its own: the new value reaches the container
through the environment, never through `docker compose exec`'s arguments. The one exception is
yours to avoid — `--password P` is visible in the host's process table to every local user for as
long as the command runs. Omit the flag and type the password at the prompt on a shared host.

## Migrations

`migrations/NNNN_<slug>.py`, applied once each, in numeric order, recorded in
`data/.neops/state.json`. These are deployment-*layout* migrations (moving a data directory,
renaming an `.env` key, a Postgres major-version upgrade), not the CMS's or the engine's own
application migrations, which each container still runs on its own boot. `0001_initial_layout` is
the only one today: it creates the `data/` tree and the state file, on both a fresh install and an
upgrade.

`./neops migrate` applies every pending migration; `--dry-run` prints what would run without
changing anything; `--fake NAME` records one migration as applied without running it. You are
asked to retype the full name to confirm, because `./neops status` will flag the deployment as
hand-patched (a `FAKED` migration in its output) afterwards. Before applying anything, a snapshot
of `.env` and the state file is written to `backups/pre-migrate-<timestamp>/`. `up` and `install`
both run `migrate` as their first real step, so a pending migration is never silently skipped.

## Upgrades

```bash
git pull        # or: git checkout <next release tag>
./neops up
```

`up` refuses to proceed if the pinned `neops-core` image would move to an *older* tag than the
last successful `up` recorded: Django migrations are not reversible, so a downgrade is refused
rather than run and corrupted. Restore a backup instead, or pass `--allow-downgrade` only if you
are certain the target schema is compatible.

## Purge

```bash
./neops purge --confirm <the exact data directory path>
```

Stops every container, then deletes `data/` and `generated/`. `.env`, `certs/`, `cust-cert/` and
`backups/` are kept. The `--confirm` argument must repeat the data directory path exactly, printed
by the command itself if you omit it. There is no separate "are you sure" prompt beyond that.
This is the only sanctioned way to wipe an installation; `docker system prune -a --volumes` on its
own removes containers and images but never touches the bind-mounted `data/`.
