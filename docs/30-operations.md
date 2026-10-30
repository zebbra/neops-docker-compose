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
judgment about which secrets to keep. The same sequence serves three cases: a fresh host, a
restore onto the release the backup was taken on, and going back to an earlier release after an
upgrade, which restores the backup taken before it (see [Going back](#going-back-to-an-earlier-release)
for what to do first). Read this page on `develop`, not the copy an older checkout leaves on
disk in step 1. It restores every database the backup holds, so everything written since
the backup is lost, in the workflow engine as well as in the CMS.

Restoring onto a host whose `neops-core` image is *older* than the one the backup was taken with
is not supported: Django migrations do not run backwards. Check out the commit the backup was
taken on; its `manifest.json` lists the images it pinned.

```bash
# 0. On a fresh host only: clone, check out the commit the backup was taken on, copy the archive in.
git clone <this repo> neops && cd neops
git checkout <the commit the backup was taken on>
mkdir -p backups && cp -a /media/<the archive>/<timestamp> backups/

# 1. Stop everything. Going back: check out the commit you noted before the upgrade.
./neops down
git checkout <the commit you noted>                # going back only

# 2. Put .env back, and forget the deployment migrations applied since the backup, so that
#    ./neops migrate and the next upgrade apply them to the restored .env again.
cp backups/<timestamp>/.env .env
DATA=./data                                        # or the NEOPS_DATA_DIR that .env sets
uv run --quiet python - backups/<timestamp>/manifest.json "$DATA/.neops/state.json" <<'PY'
import json, pathlib, sys
manifest, path = json.loads(pathlib.Path(sys.argv[1]).read_text()), pathlib.Path(sys.argv[2])
if path.exists():
    state = json.loads(path.read_text())
    for key in ("applied", "faked"):
        if key in manifest:
            state[key] = [m for m in state.get(key, []) if m["name"] in manifest[key]]
    path.write_text(json.dumps(state, indent=2) + "\n")
PY

# 3. Empty Elasticsearch and move the Postgres data aside. Elasticsearch is not in the backup:
#    emptying it removes every index, also those only a newer core created, and step 6 rebuilds
#    the ones this core uses. The directory itself stays, Elasticsearch (uid 1000) owns it.
#    pg_restore then loads into empty databases, so no table a newer release added stays behind.
[ -d "$DATA/elasticsearch" ] && sudo find "$DATA/elasticsearch" -mindepth 1 -delete
aside=postgres.before-restore-$(date -u +%Y%m%dT%H%M%SZ)
for dump in backups/<timestamp>/*.dump; do         # cms, engine and, if used, keycloak
  db=$(basename "$dump" .dump)
  [ -d "$DATA/$db/postgres" ] && sudo mv "$DATA/$db/postgres" "$DATA/$db/$aside"
done

# 4. Prepare data/ and put the key material back.
./neops migrate                                    # creates data/ if this is a fresh host
cp -a backups/<timestamp>/secrets/. "$DATA/secrets/"
cp -a backups/<timestamp>/certs/. certs/            # if the scenario uses certs/
cp -a backups/<timestamp>/cust-cert/. cust-cert/    # if you use custom CAs

# 5. Start only the databases, which initialise empty, and load the dumps. --wait matters:
#    pg_restore cannot connect while Postgres is still initialising, and `up -d` returns before it is.
#    Skip a database whose dump the manifest's "dumps" does not list (its Postgres was not running
#    at backup time): step 3 left its data in place, and there is no dump to load.
./neops render
./neops compose -- up -d --wait postgres-cms postgres-engine   # + postgres-keycloak, if used
./neops compose -- exec -T postgres-cms pg_restore -U neops -d neops --clean --if-exists \
  < backups/<timestamp>/cms.dump
./neops compose -- exec -T postgres-engine pg_restore -U postgres -d neops-workflow --clean --if-exists \
  < backups/<timestamp>/engine.dump
./neops compose -- exec -T postgres-keycloak pg_restore -U keycloak -d keycloak --clean --if-exists \
  < backups/<timestamp>/keycloak.dump             # keycloak, if used

# 6. Start the rest, which rebuilds the search indices.
./neops up                                         # going back: ./neops up --allow-downgrade

# 7. Only if `up` printed neither "neops-core checks the search index" nor "rebuilding the
#    search index" (an older core whose image did not move): rebuild by hand. Add --sync-lookups
#    if `elastic_index --help` lists it.
./neops compose -- exec -T cms python manage.py elastic_index --rebuild --force

# 8. Confirm: doctor passes, and Elasticsearch holds only the indices this core created.
./neops doctor
./neops compose -- exec -T elasticsearch curl -fsS 'http://127.0.0.1:9200/_cat/indices?v&s=index'
```

It worked when `up` (and step 7's rebuild, if you ran it) exited 0 and `doctor` passes;
`doctor` alone is no proof, since it has passed with an empty groups index after a failed `up`.
Once you are satisfied, delete the moved-aside data:
`sudo rm -rf "$DATA"/*/postgres.before-restore-*`. Until then it is the way back to the state
before the restore: stop the stack and move it back.

Step 2 rewrites `data/.neops/state.json`, which the backup does not carry, to the deployment
migrations its `manifest.json` lists. A migration applied after the backup rewrote the `.env` the
backup does not have; left recorded, it would never run on the restored `.env`.

`--if-exists` is not optional on a fresh database: without it every `DROP` in the dump fails
against a schema that does not exist yet, and `pg_restore` ends with hundreds of errors and a
non-zero exit while having loaded the data correctly — indistinguishable from a real failure.

Step 3 empties Elasticsearch instead of running `elastic_index --delete`, because of what that
command and `up` know. An older core's `--delete` deletes only the indices it defines, so a newer
core's `network` and lookup indices would keep that release's documents, which nothing reads or
updates and the next upgrade has to rebuild. And on a host that was running before the restore,
the indices still hold documents for rows the restore removed, while their stamps say they are
current, so `up`'s `elastic_index --rebuild-if-stale` would leave them alone and search and the
engine's searching locks would answer from them. Emptied, there is no index: `up`'s `cms-init`
creates this core's indices empty, and `up` then rebuilds them from the restored data, a core
with `--rebuild-if-stale` because they carry no stamp, an older core when its image or
Elasticsearch's moved since the last successful `up`. That rebuild's exit 3 means it finished
but set documents aside (see [Upgrades](#upgrades)). Do not use `--populate`: it keeps the
documents of removed rows.

### Going back to an earlier release

`up` refuses an older `neops-core` (see [Upgrades](#upgrades)); going back is the
[Restore](#restore) above, with the backup you took before the upgrade, after the steps the
release you leave asks for while it still runs. Read "Going back to an earlier release" in that
release's notes first. For neops-core 2.3.0
([its notes](https://github.com/zebbra/neops-core/blob/v2.3.0/docs/releases/v2.3.0.md#going-back-to-an-earlier-release)):

- **Before step 1, while 2.3.0 still runs**, do "First, on both, while this release still runs":
  list the facts and checks whose values disagree in type, give each one type, and export the sync
  schemas to `elastic-sync-config.json`.
- **After step 3**, on a metrics install whose earlier commit pins Grafana 11, put back the copy
  of Grafana's data that
  [Upgrading to the release with centralized logging](#upgrading-to-the-release-with-centralized-logging)
  took, since Grafana 11 cannot open the data Grafana 12 migrated. Going back between two commits
  that both pin Grafana 12 needs nothing, and the block below then does nothing. It moves the
  current data aside rather than deleting it:

    ```bash
    grafana=$(grep -ho 'grafana/grafana:[0-9]*' compose.grafana.yaml compose.metrics.yaml 2>/dev/null | head -1 | cut -d: -f2)
    if [ "$grafana" = 11 ] && [ -d "$DATA/metrics/grafana.pre-upgrade" ]; then
      sudo mv "$DATA/metrics/grafana" "$DATA/metrics/grafana.before-restore-$(date -u +%Y%m%dT%H%M%SZ)"
      sudo cp -a "$DATA/metrics/grafana.pre-upgrade" "$DATA/metrics/grafana"
    elif [ "$grafana" = 11 ]; then
      echo "no $DATA/metrics/grafana.pre-upgrade: Grafana 11 cannot start on the data Grafana 12 migrated"
    fi
    ```

    Without that copy there is no way back for Grafana's own state (its dashboards and users); the
    metrics themselves are unaffected. Delete `grafana.before-restore-*` with the Postgres copies
    once you are satisfied.

- **After step 5**, while only the databases run, load that export into the restored database:

    ```bash
    ./neops compose -- run --rm --no-deps -T cms python manage.py loaddata --format=json - < elastic-sync-config.json
    ```

    If it fails with `IntegrityError: Problem installing fixture '-'`, delete the stored schemas and
    load again, as step 3 of the notes' "neops-docker-compose, from the backup" shows.
- **In step 6**, if `up --allow-downgrade` fails with "document(s) failed to index", give the key
  one type in the earlier release's `/admin/core/elasticsyncconfig/`, using the list you made, and
  run `./neops up --allow-downgrade` again.

The notes' "from the backup" procedure is this Restore in another order. They point at "steps 1
and 2" of an earlier version of this section and then add their own steps; follow this section
from start to end instead, which covers all of them. Two differences, both on purpose: step 3
here empties Elasticsearch always, not only when the earlier commit pins an older Elasticsearch,
so the notes' final command that deletes the `network` index and the four lookup indices has
nothing left to delete; and it moves every database aside, the engine's included, since step 5
restores `engine.dump` whichever engine the earlier commit pins.

To keep the data written since the upgrade instead of restoring the backup, use the notes'
"neops-docker-compose, keeping the data written since", which migrates the database back with the
newer core; this section does not cover it.

Before the next upgrade, check out your branch again (`git checkout develop`), since step 1 left
the checkout on a commit, where `git pull` does not work.

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
application migrations, which each container still runs on its own boot. There are three today,
and all run on a fresh install as well as on an upgrade:

- `0001_initial_layout` creates the `data/` tree every bind mount points at, the state directory
  and `secrets/` at mode 0700, and hands `data/elasticsearch` to uid 1000, which the Elasticsearch
  image runs as and cannot create for itself.
- `0002_grafana_data_owner` hands `data/metrics/grafana` to uid 472, which the Grafana image runs
  as and refuses to start without. It runs whether or not you use the metrics overlay, so turning
  that overlay on later needs nothing further.
- `0003_grafana_and_logs_layout` creates `data/logs/victoria`, adds `compose.grafana.yaml` before
  `compose.metrics.yaml` in `COMPOSE_FILE`, and moves the CMS's logging keys to their new names:
  `NEOPS_LOGGER_CLASS` becomes `NEOPS_LOG_BACKEND=db` (an `OptimizedDbLogger` class is kept as
  `NEOPS_LOG_CLASS`), and `EXECUTION_LOG_RETENTION_ENABLED` becomes `NEOPS_LOG_RETENTION_ENABLED`.
  The retention horizon is carried over unchanged: `EXECUTION_LOG_RETENTION_DAYS=<n>` becomes
  `NEOPS_LOG_RETENTION=<n>d`, and an enabled retention without a days value gets `90d`, the old
  default, rather than the new `30d`.

All three are idempotent, and the first two change ownership through a throwaway container rather
than asking you for `sudo`.

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
last successful `up` recorded: an older core does not undo the migrations a newer one applied, and
may not run on them. `--allow-downgrade` lifts that refusal and nothing else; on its own it is not
enough to go back. Going back from neops-core 2.3.0 to an earlier
core is such a case: its migrations 0194 and 0195 add NOT NULL columns without a database default,
so 2.2.0 on the migrated database fails to create devices, interfaces, groups,
clients and executions (`null value in column "elastic_version"`). Restore the backup taken before
the upgrade, as [Going back to an earlier release](#going-back-to-an-earlier-release) describes, or
migrate the database back with the newer core first, as neops-core's release notes describe.

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
  nothing. **The first `up` on such a core rebuilds every index once**, because the indices an
  older core built carry no stamp. A fresh install builds its first, empty indices the same way,
  in seconds.
- **An older neops-core** (2.2.0, for example) cannot tell whether an upgrade
  changed its indices, so `up` rebuilds every index whenever the core or the Elasticsearch image
  differs from the one the last successful `up` ran (`up` logs "neops-core or Elasticsearch
  changed: rebuilding the search index"). That is `manage.py elastic_index --rebuild --force`,
  plus `--sync-lookups` where core has it.

On a core with `--rebuild-if-stale`, a document Elasticsearch refuses does not fail the rebuild:
core finishes it, sets the refused documents aside and marks their rows `rejected`, and exits 3.
`up` then prints a `WARNING` and, where core can tell, how many rows are marked `rejected` and the
command that lists them
(`./neops compose -- exec -T cms python manage.py index_sync_status --list rejected --limit 0`;
`--limit 0` lists every device, interface, group and client row, where core's default stops
at 100 per model), and records the start as usual. Keep the rebuild's output: it is where the reason for each refusal is printed.
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
stops before recording the start, so the next `./neops up` tries it again, and search stays
partial until a rebuild succeeds. On an older core a document Elasticsearch refuses is such a
failure, and fails every retry the same way. The rebuild stops at the first type with refused
documents, and its output names each of them by `_id` (`device-6` is device 6), with
Elasticsearch's reason. Such a row is not marked `rejected`, so neither `index_sync_status` nor
`./neops doctor` shows it: correct its data and save it, then run `./neops up` again, which may
stop at refused documents of a later type.

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

On a core with `--rebuild-if-stale`, `--rebuild` stamps the indices it rebuilt, also when it set
documents aside, so the next `up` leaves them alone; `--rebuild --models` stamps only those
models' indices, and `--populate` stamps nothing. A hand-run `--rebuild-if-stale` that exits 3
finished, but set documents aside as described above.

### Upgrading to the release with centralized logging

This release moves the engine to 0.44 and the worker to 0.3, which are compatible only with each
other: a running 0.2 worker fails every job it reports to a 0.44 engine. Upgrade with no workflow
run in progress, and stop the worker first:

```bash
DATA=${NEOPS_DATA_DIR:-./data}                         # the value from .env, if it sets one
./neops compose -- stop worker
./neops backup
cp -a "$DATA/.neops/state.json" "$DATA/.neops/state.json.pre-upgrade"   # the backup does not carry it
./neops compose -- stop grafana                                         # metrics installs only:
sudo cp -a "$DATA/metrics/grafana" "$DATA/metrics/grafana.pre-upgrade"  # a stopped Grafana's copy
git pull
./neops up                                    # starts the new worker and Grafana again
```

Grafana moves from 11 to 12 and migrates `metrics/grafana` under the data directory on its first
start; there is no way back to 11 on the migrated directory, so the copy above, taken while Grafana
is stopped, is the only rollback for Grafana's state.
On the first `up`, `./neops check` prints one `WARNING:` that `compose.metrics.yaml` requires
`compose.grafana.yaml`; migration `0003` then adds it. The CMS's retention default drops from 90 to
30 days for installs that never enabled retention; one that had it enabled keeps its horizon
(`0003` writes it out).
`./neops render` now writes Grafana's log datasource (`generated/grafana/log-datasources.yml`),
following the log backend, in place of `logs/grafana/datasources/victorialogs.yml`, which is
gone; `./neops up` renders it before Grafana starts, so there is nothing to do by hand.

Elasticsearch upgrades its data directory in place on the first start of a newer image and cannot
open it with an older one afterwards. Going back to a previous release therefore needs
`data/elasticsearch` emptied first; it is derived data, and the rebuild above refills it.

## Engine limits

The engine reads these from `.env`. Unset or empty keeps the engine's default; run `./neops up`
after changing one. `ROLLBACK_DATA_MAX_BYTES` takes a byte count or a size with a unit, such as
`5MiB`, `100kb` or `1.5mb`; units are binary, so `5MiB` and `5mb` are both 5242880 bytes. The
others take a plain integer in the unit shown. `0` also keeps the default. A value the engine
cannot parse, such as `30d`, `1.5e6` or a negative `ROLLBACK_DATA_*` value, stops it from
starting, and `docker compose logs engine` names the variable.

Engines before 0.44.1 read only the leading digits of these values, so `5MiB` was parsed as 5
bytes. If you pin an older engine, write plain integers.

| Variable | Unit | Default | What it bounds |
|---|---|---|---|
| `ROLLBACK_DATA_MAX_BYTES` | bytes | `5242880` (5 MiB) | the rollback data one job attempt may store; larger data is dropped |
| `ROLLBACK_DATA_RETENTION_DAYS` | days | `30` | how long a run in `FAILED_UNSAFE_ACK` keeps its rollback data before it is purged |
| `ROLLBACK_DATA_SWEEP_INTERVAL_MS` | ms | `3600000` (1 h) | how often that purge runs |
| `BLACKBOARD_PENDING_JOB_TIMEOUT` | ms | `3600000` (1 h) | how long a job no worker has polled waits before the engine fails it |

## Purge

```bash
./neops purge --confirm <the exact data directory path>
```

Stops every container, then deletes `data/` and `generated/`. `.env`, `certs/`, `cust-cert/` and
`backups/` are kept. The `--confirm` argument must repeat the data directory path exactly, printed
by the command itself if you omit it. There is no separate "are you sure" prompt beyond that.
This is the only sanctioned way to wipe an installation; `docker system prune -a --volumes` on its
own removes containers and images but never touches the bind-mounted `data/`.
