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
| `./neops secrets` | fill every blank generated secret in `.env` (database passwords, the Django secret key, the admin passwords); never overwrites |
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
# On a neops-core whose `elastic_index --help` lists --rebuild-if-stale:
./neops compose -- exec cms python manage.py elastic_index --delete --force
./neops compose -- exec cms python manage.py elastic_index --rebuild-if-stale
# On an older core instead:
./neops compose -- exec cms python manage.py elastic_index --rebuild --force

# 4. Confirm.
./neops doctor
```

`--if-exists` is not optional on a fresh database: without it every `DROP` in the dump fails
against a schema that does not exist yet, and `pg_restore` ends with hundreds of errors and a
non-zero exit while having loaded the data correctly — indistinguishable from a real failure.

The indices need no `elastic_index --create`: `up` runs `cms-init`, which creates any that are
missing. On a host that was running before the restore, the indices still hold documents for rows
the restore removed, and their stamps still say they are current, so `up` leaves them alone. Deleting
them makes every index stale: `--rebuild-if-stale` then refills all of them (groups, clients and the
lookup indices included) and stamps them, so the next `up` has nothing to do. Its exit 3 means it
finished but set documents aside (see [Upgrades](#upgrades)). On a fresh host this
costs one rebuild more than strictly needed, since `up` has just built them from the restored
data. An older core has no stamps; `--rebuild --force` drops the old documents and refills every
index. Do not use `--populate`: it keeps the documents of removed rows.

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
application migrations, which each container still runs on its own boot. There are two today, and
both run on a fresh install as well as on an upgrade:

- `0001_initial_layout` creates the `data/` tree every bind mount points at, the state directory
  and `secrets/` at mode 0700, and hands `data/elasticsearch` to uid 1000, which the Elasticsearch
  image runs as and cannot create for itself.
- `0002_grafana_data_owner` hands `data/metrics/grafana` to uid 472, which the Grafana image runs
  as and refuses to start without. It runs whether or not you use the metrics overlay, so turning
  that overlay on later needs nothing further.

Both are idempotent, and both change ownership through a throwaway container rather than asking
you for `sudo`.

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

Once the stack is healthy, `up` asks neops-core whether the search index still matches it and
Elasticsearch, and rebuilds it if not. It runs in `cms` rather than in `cms-init` on purpose: `cms`
waits for `cms-init`, so a rebuild there would take the whole CMS down, not only search. Its
output streams to your terminal. Which of two paths `up` takes depends on the core, and `up` says
which; to check yourself:

```bash
./neops compose -- exec cms python manage.py elastic_index --help | grep -- --rebuild-if-stale
```

- **A neops-core with `--rebuild-if-stale`** decides itself (`up` logs "neops-core checks the
  search index and rebuilds what is stale"). Every index carries a stamp of the definition it was
  built from (names, mappings and settings, including the `ELASTICSEARCH_DSL_INDEX_*` values in
  `compose.yaml`'s `x-core-env`, plus core's own content version) and of the Elasticsearch version
  that built it. Core rebuilds the indices whose stamp is missing or differs, plus the ones that
  share documents with them or that they mirror: a stale lookup index also rebuilds `network`,
  `groups` and `clients`, which is close to a full rebuild. When every stamp matches it does
  nothing. **The first `up` on such a core rebuilds every index once**, because indices built by
  compose or by a hand-run `--rebuild` carry no stamp. A fresh install builds its first, empty
  indices the same way, in seconds.
- **An older neops-core** (2.2.0, pinned by this release, is one) cannot tell whether an upgrade
  changed its indices, so `up` rebuilds every index whenever the core or the Elasticsearch image
  differs from the one the last successful `up` ran (`up` logs "neops-core or Elasticsearch
  changed: rebuilding the search index"). That is `manage.py elastic_index --rebuild --force`,
  plus `--sync-lookups` where core has it.

On a core with `--rebuild-if-stale`, a document Elasticsearch refuses does not fail the rebuild:
core finishes it, sets the refused documents aside and marks their rows `rejected`, and exits 3.
`up` then prints a `WARNING` and, where core can tell, how many rows are marked `rejected` and the
command that lists them
(`./neops compose -- exec cms python manage.py index_sync_status --list rejected`), and records
the start as usual. Keep the rebuild's output: it is where the reason for each refusal is printed.
Those rows stay out of search until their data is corrected and the row saved, or until a later
rebuild finds Elasticsearch accepting them. What to do about them is in the
[rows rejected by Elasticsearch](50-troubleshooting.md#search-index-out-of-sync) bullet of
*Search index out of sync*.

A full rebuild took 57 s for 2,000 devices, 20,000 interfaces and 50 groups; plan an upgrade that
rebuilds as a short maintenance window, because while it runs:

- it deletes the indices before it recreates them, so searches fail with
  `index_not_found_exception` for a moment, then return partial results until it finishes;
- the groups index stays empty until the end;
- relation searches (`groups.*`), scope filters and workflow acquires fail for the whole rebuild,
  because the lookup indices they join against are recreated last;
- restricted users may see fewer results until the rebuild has stamped their rows as synced.

A save that lands while an index is deleted fails instead of recreating it: Elasticsearch is
configured not to auto-create the Neops indices (`action.auto_create_index`), since an
auto-created one would carry a dynamic mapping that breaks search. The rebuild re-indexes those
rows from the database anyway. If the rebuild fails, which is any non-zero exit other than
`--rebuild-if-stale`'s exit 3 above (on an older core, any non-zero exit at all), `up` says so and
stops before recording the start, so the next `./neops up` tries it again.

**Do not interrupt a rebuild.** Ctrl-C stops `./neops up`, not the rebuild: `docker compose exec`
cannot signal the process it started, so it keeps running inside `cms`. On a core with
`--rebuild-if-stale`, every `elastic_index` command that creates, deletes or fills indices takes
the same lock, so the next `./neops up` waits for that rebuild at `cms-init` with nothing on the
screen, and can fail with a compose timeout once that wait passes 15 minutes. On an older core
nothing waits, and the next `up` starts a second full rebuild beside the first. Either way:

```bash
./neops logs cms-init      # follows; with --rebuild-if-stale, "creating the search indices (...)" is last
./neops compose -- top cms # a python ... elastic_index process is the rebuild still running
```

Wait for it to finish, or abort it with `./neops restart cms`. On a core with
`--rebuild-if-stale`, the indices it did not finish carry no stamp, so the next `./neops up`
rebuilds them, and the engine's searching locks stay refused until core notices the aborted
rebuild (its heartbeat goes stale after 5 minutes). On an older core the next `up` rebuilds
everything again only if the core or Elasticsearch image moved since the last successful `up`.

To run the check or a rebuild by hand:

```bash
./neops compose -- exec cms python manage.py elastic_index --rebuild-if-stale
./neops compose -- exec cms python manage.py elastic_index --rebuild --force
```

`--rebuild` writes no stamps, so on a core with `--rebuild-if-stale` the next `up` rebuilds once
more. A hand-run `--rebuild-if-stale` that exits 3 finished, but set documents aside as described
above.

Elasticsearch upgrades its data directory in place on the first start of a newer image and cannot
open it with an older one afterwards. Going back to a previous release therefore needs
`data/elasticsearch` emptied first; it is derived data, and the rebuild above refills it.

## Purge

```bash
./neops purge --confirm <the exact data directory path>
```

Stops every container, then deletes `data/` and `generated/`. `.env`, `certs/`, `cust-cert/` and
`backups/` are kept. The `--confirm` argument must repeat the data directory path exactly, printed
by the command itself if you omit it. There is no separate "are you sure" prompt beyond that.
This is the only sanctioned way to wipe an installation; `docker system prune -a --volumes` on its
own removes containers and images but never touches the bind-mounted `data/`.
