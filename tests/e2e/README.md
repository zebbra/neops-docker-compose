# End-to-end scenario runs

`run_scenario.py` installs one `examples/*.env` scenario for real — containers, images,
databases — in a throwaway clone of this checkout, asserts the deployment works, and brings
it down again. It is the only test in this repo that touches Docker; `make check` does not
run it.

```bash
make e2e SCENARIO=traefik-tls-selfsigned
uv run python tests/e2e/run_scenario.py external-proxy --port-base 18000
uv run python tests/e2e/run_scenario.py traefik-tls-selfsigned --keep --workdir /tmp/neops-e2e/tls
uv run python tests/e2e/run_scenario.py tests/e2e/examples/loki.env --port-base 22500
```

The scenario is a name from `examples/`, a name from `tests/e2e/examples/` (test-only
scenarios such as `loki`), or the path of an `.env` file.

A run takes 5 to 10 minutes once the images are cached; the first one pulls about 3 GB.
`cms-init` on a cold Elasticsearch takes roughly two minutes and the CMS healthcheck has a
150 s start period, so a stack that looks stuck usually is not. Watch it with
`./neops compose -- logs -f` inside the clone printed as `workdir ...`.

## What it asserts

| Assertion | Scenarios |
|---|---|
| `install` completes and doctor is green | all except expose mode |
| the two worker API deny probes (job routes, worker log route) are doctor's **only** warnings | expose mode (`compose.expose.yaml`) |
| the admin user can log in and gets an access token | password-login scenarios |
| the Django admin works over plain http: no HSTS, neither cookie is `Secure`, and the login form POST is accepted | `http://` scenarios |
| the admin creates a device group over GraphQL, reads it back and deletes it, which is what the seeded admin role buys | password-login scenarios |
| the OIDC providers are seeded and visible in `appSettings` | OIDC scenarios |
| core's admin, `/djstatic/` and the engine's `/engine/health` answer on the one hostname | shared-host |
| the worker container runs and its log shows function blocks registering | all |
| `cms-worker` and `cms-beat` run and are healthy under `COMPOSE_PROFILES=cms-tasks`, and do not exist without it | all |
| a device created by hand on the seeded `Linux Generic` platform, a `generic-static-facts` task executed on it over GraphQL, run by `cms-worker` to `SUCCESSFUL`, and the fact read back from the device | `cms-tasks` profile (`local-legacy`) |
| every exporter, VictoriaMetrics, vmalert and Grafana run; Grafana answers `/api/health` on its public URL; every scrape target reports `up` | metrics |
| the log store runs and answers the engine (VictoriaLogs `/health`, Loki `/ready`) and no other store's container exists, the worker container cannot reach it while it does reach the engine's `/health`, Grafana provisions the store's datasource (VictoriaLogs, or Loki when Grafana runs), the engine's `GET /version` reports `logs.state` `ok`, a `group_save_fact` run (`workflows.py`) reaches `completed_ack` and `GET /workflow-execution/:id/logs?includeCore=true` returns its engine and worker lines, each carrying the run's `workflow_package` / `workflow_name` / `workflow_version` and, on a job's lines, the job's `function_block_package` / `function_block_name` / `function_block_version`, core's audit line of the change the unlock applied (`lock <ref>: applied PATCH on group <id> -- actor: …`; core logs nothing for a lock it grants) and no other run's, under Loki no record carries `service_name` or `detected_level`, `GET /workers` answers the admin 200, and the worker's own lines come back from `GET /workers/:uuid/logs` | a log store: `logs`, `metrics-logs` (VictoriaLogs), `loki` (Loki, no VictoriaLogs check) |
| the engine reports no queryable store and a log read answers 503 `LOGS_NOT_CONFIGURED` | no log store |
| `./neops backup` writes `cms.dump` and `engine.dump` | all |
| a second `install` mints no second API key | all |

## Upgrade run

```bash
make e2e-upgrade FROM=<previous release tag>
```

Installs `examples/metrics.env` as the release `FROM` shipped it, with
`EXECUTION_LOG_RETENTION_ENABLED=True` and no days value, then checks out this commit in the same
clone, as `git pull` would, and runs `./neops up`. It asserts that `./neops check` passes on the
old `.env` with exactly one `WARNING:` line, that `up` succeeds with doctor green, that migration
`0003` is recorded, that `.env` names `compose.grafana.yaml` before `compose.metrics.yaml` and
carries `NEOPS_LOG_RETENTION=90d` and `NEOPS_LOG_BACKEND=db`, and that Grafana still lists the
dashboards it had before. Run it on every release branch before tagging.

Any other scenario upgrades the same way: `run_scenario.py local --upgrade-from <ref>` installs the
`local` shape the live deployment runs, and there `check` must warn about nothing and
`COMPOSE_FILE` must come out unchanged. The older release's doctor may only know the job-route
deny probe, so its install warns once, not twice.

`--hold-job` adds chaos C12b to an upgrade run (`upgrade_hook.py`): on the old install it stops
the worker, starts the e2e workflow and polls its first job on the stopped worker's behalf, through
the engine container, so the job is held but never reported. After the upgrade a result for it must
be refused `403 LEASE_MISSING`, and the run must end failed once the engine cleans up the worker
that vanished. The held job is recorded in `<workdir>/held-job.json`.

VictoriaMetrics publishes no host port, so the scrape-target assertion asks it over
`wget` inside its own container and waits for every target to have been scraped once
(the interval is 30 s).

Core allows five logins a minute per address and every doctor run spends two, so the second
install waits until a minute has passed since the last login the harness caused; a run in expose
mode, where doctor runs twice per install, would otherwise be rate-limited into a failure.

The device for the task assertion is created on the seeded `Linux Generic` platform because
Nornir's inventory only holds devices whose platform carries a `nornir` library key. Any other
device is left out (the worker log says `0 hosts selected`), and the execution still ends
`SUCCESSFUL` with nothing written.

In expose mode there is no proxy in front of the stack, so the engine's public worker routes
really are reachable and doctor says so. That is the documented contract of
`examples/external-proxy.env`, not a defect: the harness requires that probe to fail and
every other probe to pass.

## Ports and hostnames

Every published port is derived from `--port-base` (default 18000), so several scenarios can
run at once on different bases and none of them collides with whatever already owns 80/443:

| Port | Role |
|---|---|
| base + 80 | Traefik HTTP, or the web client in expose mode |
| base + 443 | Traefik HTTPS |
| base + 444 | the monitor entrypoint in shared-host mode, when the monitor has its own port |
| base + 180 | Keycloak's loopback port |
| base + 300 | Grafana's loopback port |
| base + 0 / 30 / 31 | CMS, engine and monitor in expose mode |

Hostnames come from the example with `neops.example.com` rewritten to `neops.localhost`.
Browsers and the harness resolve anything under `.localhost` to 127.0.0.1, so no DNS entry
and no `/etc/hosts` edit is needed. The harness still passes `--connect 127.0.0.1` because
Python's resolver does not special-case `.localhost` on every libc. Public URLs carry the
non-default port, for example `https://neops.localhost:18443`.

Each clone also gets `COMPOSE_PROJECT_NAME=neops-e2e-<scenario>-<base>`: compose otherwise
names the project after the directory, and every clone is called `repo`.

## Certificates

Every `tls-files` example is switched to `NEOPS_TLS_SELF_SIGNED=true` with the certificate
under `data/secrets/tls/`, so no run needs an operator-supplied certificate. The probes use
`--insecure`.

## The monitor image

The harness runs the published `quay.io/zebbra/neops-monitor-app` tag pinned in `compose.yaml`. To
exercise a local build instead, pass `--extra-env NEOPS_MONITOR_IMAGE=neops-monitor-app:local`;
`Compose.pull()` passes `--ignore-pull-failures`, so a local-only tag does not abort the start.

## The override file

The harness writes a `compose.override.yaml` into the clone and appends it to `COMPOSE_FILE`.
It disables Elasticsearch's disk allocation thresholds, because a host above the 95% flood
stage turns every index read-only and `cms-init` then fails in a way that has nothing to do
with the change under test. The file is gitignored and is exactly what it is there for.

A test-only overlay reaches Compose through the same file. `check` refuses any unshipped file
in `COMPOSE_FILE`, so a scenario names its overlays in the harness-only key `E2E_OVERLAYS`,
which never reaches the clone's `.env`, and the harness merges them into
`compose.override.yaml`; their relative paths therefore resolve from the clone root. The Loki
scenario (`tests/e2e/examples/loki.env`) works this way: `compose.loki-test.yaml` adds
`grafana/loki` on its own `logs` network, and the scenario's `.env` sets `NEOPS_LOG_BACKEND=loki`
and `NEOPS_LOG_URL`, the path an operator with their own Loki takes. Loki keeps its default
`discover_service_name` and `discover_log_levels` on purpose: the engine must drop the
`service_name` and `detected_level` keys they add, and S12 and the key-set diff prove it does.
`make compose-config` covers the e2e scenarios too.

## Workflow runs

`workflows.py <deployment dir>` publishes `wf.e2e.neops.io/group_save_fact` (one step,
`fb.base.neops.io/group_save_fact:0.1.0`, `increment` so a rerun never answers 409), runs it on the
device group `e2e-log-run` and polls it until it settles: `completed_ack`, `failed_safe_ack` or
`failed_unsafe_ack`, the states the engine reaches only once it has unlocked the group in the CMS.
The unlock applies the fact, and core writes an audit line for it, so one run carries engine,
worker and core lines. Its token is issued by the CMS for
`NEOPS_ADMIN_USER` through `docker compose exec cms`, which spends no login.

## Smoke suite

```bash
uv run python tests/e2e/smoke.py --base /path/to/deployment [--connect 127.0.0.1 --insecure] \
  [--min-dashboards N] [--record-schema ../neops-workflow-engine/schema/LogRecord.schema.json] \
  [--dump-keys keys.json]
```

S1-S13 of the logging test plan, one line each, against any installed deployment, live or a
`--keep` clone. Read-only except S7 (one workflow run) and S9 (a CMS user without any workflow
grant, created for the check and deleted again). A check that does not apply to the
deployment's store prints `N/A`; any `FAIL` exits 1. S12 validates the S7 records against the
engine's `LogRecord.schema.json`, which the engine image does not ship: the default is the
sibling engine checkout. `--dump-keys` writes the key set of the S7 records, for the
VictoriaLogs/Loki comparison.

## Image-ID guard

```bash
uv run python tests/e2e/image_guard.py record <clone> <workdir>/ids-pre.json [--ref IMAGE ...]
uv run python tests/e2e/image_guard.py verify <clone> <workdir>/ids-pre.json
```

`up` pulls with `--ignore-pull-failures`, so a local retag under a published name is silently
replaced the moment that tag is published. `record` stores the id each image reference resolves
to (every image of the clone's compose config by default); `verify` fails when one moved or a
container started from a recorded reference runs another id. A run whose guard fails is void.

## The Keycloak browser login

`run_scenario.py oidc-keycloak` only proves that the providers are seeded. The sign-in itself
is a browser flow — three redirects across two origins, a one-time code and a role claim — so
it lives in `keycloak_login.py` and runs against a stack left up with `--keep`:

```bash
uv run --with playwright playwright install chromium          # once per machine
uv run python tests/e2e/run_scenario.py oidc-keycloak --port-base 20000 --workdir /tmp/kc \
  --keep --extra-env NEOPS_ES_HEAP=512m
uv run --with playwright python tests/e2e/keycloak_login.py /tmp/kc/repo
```

It creates the realm user `e2e-keycloak` and a `neops-e2e` role on the `neops-auth` client
through Keycloak's admin REST API on the loopback port (`--port-base` + 180, plain HTTP, so no
trust store is involved), then drives Chromium with `ignore_https_errors` through the web
client's "Login with Keycloak" button. It asserts that the app leaves `/login`, that
`localStorage.token` holds a bearer token, and that `resource_access.neops-auth.roles` reached
core — the authorization-critical half, which a login that merely succeeds does not prove.
Screenshots and page HTML from a failure land in `<workdir>/playwright/`.

Two flags skip the browser and only talk to the admin API: `--create-realm-role NAME` and
`--assert-realm-role NAME`, which is how a chaos run shows that `down` + `up` does not
re-import the realm over what the admin console holds.

Chromium resolves `*.localhost` to 127.0.0.1 natively, and the wait for the token tolerates a
`SecurityError` while the main frame is still an opaque mid-navigation document.

## The external proxy

`run_scenario.py external-proxy` proves the stack works with its ports published and nothing in
front of them. `external_proxy_check.py` puts a real proxy there and checks the contract in
[docs/40-external-proxy.md](../../docs/40-external-proxy.md), against a stack left up with
`--keep`:

```bash
uv run python tests/e2e/run_scenario.py external-proxy --port-base 22000 \
  --workdir /tmp/neops-e2e/proxy --keep --extra-env NEOPS_ES_HEAP=512m
uv run python tests/e2e/external_proxy_check.py /tmp/neops-e2e/proxy/repo --caddy-port 22443
```

It runs `caddy:2` on the host network from `caddy/Caddyfile`, which is the documented Caddy
snippet with the harness's hostnames, ports and `tls internal`, then repoints the clone's public
URLs at it and runs `./neops render && ./neops up`.

| Assertion | Why it is not obvious |
|---|---|
| doctor is fully green, deny probe included | the same run without a proxy fails that one probe by design |
| every spelling of the worker routes answers 403 with an empty body | Caddy's own 403 has no body, the engine's has one, which is how the two are told apart |
| `/health`, `/workers`, `/function-blocks/registrations/list` and `/blackboard/jobs` still reach the engine | the monitor app needs them, so the deny rule must be exact |
| `GET` on a denied path and `POST /workers` are not blocked | the rule is `POST`-only and anchored |
| a 1 MB body reaches the CMS, and the adapted config carries the 200 MB limit | 200 MB is asserted on `caddy adapt`, not sent |
| a `graphql-ws` upgrade on `/graphql` answers `101` | subscriptions break silently through a proxy that drops the hop-by-hop headers |
| the cms container really has `RATELIMIT_IP_META_KEY` | without it `--probe-ratelimit` passes while testing nothing |
| `--probe-ratelimit` passes and a forged `X-Real-IP` is still rate-limited | proves the proxy overwrites the header rather than passing the client's through |

The rate-limit half waits out a minute first: core allows five logins a minute per address and
doctor spends two of them on every run.

## Chaos runs

`chaos.py` takes the clone a `--keep` run left behind and breaks the deployment on purpose. It
refuses any other directory: the clone's `.env` must name a `neops-e2e-*` project, its
`compose.override.yaml` must carry the harness header, and `COMPOSE_PROJECT_NAME` and `COMPOSE_FILE`
must not be set in the shell, where Compose would prefer them over the clone's `.env`.

```bash
uv run python tests/e2e/run_scenario.py traefik-tls-selfsigned --port-base 19000 \
  --workdir /tmp/neops-e2e/chaos --keep --extra-env NEOPS_ES_HEAP=512m
uv run python tests/e2e/chaos.py /tmp/neops-e2e/chaos/repo
```

| Step | Name | Assertion |
|---|---|---|
| kill `cms`, `engine`, `redis`, `postgres-cms`, `worker`, `traefik` in turn | `kill` | `compose up -d --wait` brings each back and doctor is green again |
| `./neops down` then `./neops up` | `restart` | the admin still logs in, a device group written before the stop reads back and deletes again, `data/secrets/engine.env` is byte-identical, `state.json` still holds one API key and doctor is green |
| the same with a host-wide prune in the middle | `prune` | everything `restart` asserts, plus the prune emptied the host and `up` fetched every image again |
| three corrupted `.env` files | `env` | `./neops check` exits 1 and names each problem, and passes again once restored |
| `./neops restart engine` under a polling worker | `engine` | the engine goes healthy again, the worker container survives and doctor is green |
| `compose stop postgres-cms` | `database` | logins fail while the database is gone and work again when it returns |
| with the logs overlay, `kill` also kills `victorialogs` and `grafana` (C1) | `kill` | the store runs and `GET /version` reports it `ok` within 30 s; Grafana still has the datasource |
| `NEOPS_LOG_RETENTION=90` (C4b) | `env` | `check` exits 1 and says the unit is required |
| loadgen.py at 300 lines/s for 60 s | `loadgen` | it sends all 18,000 lines within 62 s and every one reaches the store once |
| the log-store faults C2-C12a, C14, C16 and the C15 report | `c2` … `c16` | see below |

With `compose.logs.yaml` in the clone's `COMPOSE_FILE` the default run adds the log-store steps
of `chaos_logs.py`, C14 last. They follow the plan's injection rules: an engine fault is written
into the clone's `compose.override.yaml` (the last file, so it beats `compose.logs.yaml`), the
engine is recreated alone and every injected value is read back with `printenv` first; recovery
restarts single services with `--no-deps`, never a bare `up`; assertions read `GET /version`; seq
duplicates are checked per series (engine and core per process, a worker per job) on the records
VictoriaLogs holds for the step's window, and holes only in the workers' series: a store probe that
fails takes an engine seq and is never spooled, so an outage leaves engine holes by design, and
those are printed instead. Traffic "under load" comes from `chaos_kit.make_load()`: a `loadgen.py`
burst at 300 lines/s that outlasts the step's fault by 30 s (see [Load generator](#load-generator)).
`--load workflows` swaps in back-to-back `workflows.py` runs on two to four threads, for a worker
image without the SDK's push sink, and leaves `loadgen`, `c9` and `c11` out of the default run.
C13 needs a skewed worker clock and is deferred (D8).

| Step | Fault | Assertion |
|---|---|---|
| `c2` | `compose stop victorialogs` 120 s under load | `open`, outbox rows > 0, then `ok` with the outbox drained, no line dropped and `outbox.dropped` 0, every burst line stored once (the outage's runs readable, under `--load workflows`), no duplicate seq and no worker seq hole; prints the replay time |
| `c3` | one run opens the circuit, then no traffic | the run's lines are held; no replay attempt and no row deleted while open; drains afterwards |
| `c4` | `NEOPS_LOG_URL=http://engine:3030`, confirmed to answer 404 first | `misconfigured`, doctor fails on the log-store probe, the outbox holds the refused lines and replays them once restored |
| `c4a` | `NEOPS_LOG_URL=http://10.255.255.1:9428` | `open`, not `misconfigured`; doctor passes with a warning |
| `c5` | the same, then `./neops up` | `up` exits 0, the engine is healthy, the store `open` |
| `c6` | the engine disconnected from `<project>_logs` 60 s under load | as `c2` |
| `c7` | `compose restart engine` in the middle of a `c2` outage | the store `open` during the fault, outbox rows > 0 that survive the restart, then `ok` with the outbox drained, no line dropped by the engine and `outbox.dropped` 0. Under loadgen the restarting engine takes no pushes, so the SDK may overflow its buffer: loadgen loses nothing else, its series has as many seq holes as the SDK dropped, the stored `dropped N log lines` lines add up to that N and its newest burst line is stored; every other worker's series has no hole, and no series a duplicate |
| `c7b` | `compose kill -s KILL engine` there instead | outbox rows survive, no duplicate seq; holes are printed, not failed: the killed engine loses worker lines it had accepted too |
| `c8` | `NEOPS_LOG_OUTBOX_MAX_ROWS=2000`, a `c2` outage | `outbox.dropped` > 0, the engine logs the drop, and loadgen's series misses no more lines than `outbox.dropped`. The outbox dropped its oldest rows: some of the burst lines logged 6-3 s before the engine reported the store `ok` again are stored, and every missing burst line is older than the oldest stored line of the outage (by burst index, from the burst's start and its rate). Under `--load workflows`, only the newest run's lines being readable is checked |
| `c9` | loadgen at 400 lines/s for 90 s, `compose stop engine` for 75 s, back with `up -d --no-deps engine` | the SDK reports N > 0 dropped lines and nothing lost otherwise; by `seq` over loadgen's series the store holds sent - N with no duplicate and the newest burst line; the stored `dropped N log lines (buffer full)` lines add up to N. The SDK dropped its oldest lines: some of the burst lines logged 6-3 s before the engine's `/health` answered again from inside its container (or before the burst ended, if that came first) are stored, and every missing burst line is older than the oldest stored line of the outage |
| `c10` | `postgres-engine` and `victorialogs` stopped 30 s under load | the engine keeps running and answers a worker push 503, everything recovers, and the SDK logged a retry of a 503 |
| `c11` | `docker kill -s KILL` the loadgen container 20 s into a burst | it died without a summary; a fresh loadgen worker (`run --no-deps`) is `ONLINE` in `GET /workers` within 60 s of the kill and runs its job; the killed worker's stored series have no hole and no duplicate, so no batch was stored in part or twice |
| `c12a` | `NEOPS_WORKER_IMAGE=…neops-worker-sdk:0.2.0-beta.8` | precondition: GET /version reports 0.44.x (an engine built without `NEOPS_VERSION` reports `develop` and accepts every client); the old worker exits and logs `… is not compatible with this engine`; the current one runs again |
| `c15` | `pkill -f celery.*worker` in `cms-worker` during a run | report only, and only with the `cms-tasks` profile |
| `c16` | 10 baseline runs, then 10 runs with `victorialogs` paused for 100 s | no run slower than the baseline's worst plus 1 s, and after the pause every paused run's page holds core's unlock audit line, so core did push during the pause |
| `c14` | `victorialogs` paused; the engine swapped to `0.43.0-beta.4` and back | the old engine boots on the new schema and leaves `log_outbox` alone; the rows replay after the re-upgrade |

### Load generator

`loadgen.py` runs inside the worker image as a one-off container of the clone's `worker` service,
started by `loadgen_driver.py` as

```bash
./neops compose -- run --rm --no-deps -T --name <project>-loadgen-<id> \
  -v <checkout>/tests/e2e/loadgen.py:/loadgen.py:ro worker /app/.venv/bin/python /loadgen.py --rate N --seconds S
```

`<checkout>` is the tree the harness runs from, not the clone: the container runs the same
`loadgen.py`, uncommitted edits included, whose markers the driver parses.

It runs the SDK's own worker loop (`neops_worker.main_async`) with `DIR_FUNCTION_BLOCKS` unset, so
the only block it registers is its own `fb.e2e.neops.io/log_burst:0.1.0`, which no other worker
has. The driver publishes `wf.e2e.neops.io/log_burst` (one global step on that block) and runs it;
the block then logs N lines a second for S seconds through the job's logger, so every line takes
the production path: the push sink's 10,000-line buffer, its overflow and its retries. Once the
burst is out and the engine answers `/health`, loadgen waits `SETTLE_SECONDS` and sends itself SIGTERM; the
SDK's graceful shutdown pushes the rest. It prints `LOADGEN READY`, `LOADGEN BURST` and, at exit,
`LOADGEN SUMMARY` with the worker id, the lines sent, and what the SDK logged about lines it did
not deliver: dropped (`dropped N log lines (buffer full)`), unpushed at exit, refused by the
engine, lease refusals, and the HTTP statuses it retried. loadgen recognises those messages by
their wording, and an SDK that rewords one changes the result in one of two ways. A reworded
registration, retry or drop message fails the run loudly: no `READY`, no retry status, no drop
warning in the store. A reworded unpushed, refused or lease message would count 0 without a sound,
so before it starts loadgen reads the installed SDK's source and refuses with exit 2 when one of
those three (`loadgen.QUIET_NOTICES`) is no longer in it. `tests/unit/test_loadgen.py` only
tests the parsing against sample messages and cannot notice an SDK change.

The block is neither idempotent, pure nor reversible, so the engine neither replaces the job of
a killed loadgen nor rolls it back, and no stale job waits for the next loadgen on the same stack. loadgen is one more worker: with
the developer-preview engine image, which allows two, C11's fresh loadgen is refused with 403.

The driver refuses any directory but a harness clone, with the same guard as `chaos.py`. The
accounting reads the loadgen worker's records straight from VictoriaLogs (`process` is the worker
id) and counts per `seq` series: each starts at 0, so the highest `seq` stored says how many the SDK
numbered.

`--only` runs the named steps in the order given, which is how the prune is driven on its own:

```bash
CHAOS_PRUNE=yes uv run python tests/e2e/chaos.py /tmp/neops-e2e/chaos/repo --only prune
```

A green run takes about five minutes, most of it inside `up -d --wait`: killing `redis` or
`postgres-cms` recreates everything that depends on them and re-runs `cms-init`, and the
`down` plus `up` pulls every image again. A run with failures takes longer, because the waits
spend their whole timeout. Each step prints `PASS`/`FAIL` with its own elapsed time, and a
step that raises does not stop the ones after it, so one run reports everything that is broken.

Three things the run accommodates, each a property of the product rather than a defect:

- **Core rate-limits login to five a minute per address** and every doctor run spends two, so
  a doctor that fails *only* on the worker probe with a rate-limit message is retried after a
  minute instead of being believed.
- **The restart step writes a device group, not a device.** A device would need the worker and
  a platform row that no scenario seeds, so the row it carries across the stop is a group. The
  admin role that makes the write legal is seeded by `cms-init`; `run_scenario.py` asserts that
  separately, under its own group name, so the two can run against one clone.
- **An engine restart costs no re-registration.** The engine keeps worker registrations in its
  own Postgres, so the worker logs nothing across the restart and simply keeps polling.

### The prune step

`--prune` swaps the `prune` step in for `restart`, and `--only prune` runs it alone. It is
`restart` with `docker system prune -a --volumes` between the `down` and the `up`, and it adds
three assertions the plain restart cannot make:

- every image the last `up` ran is gone, except `NEOPS_MONITOR_IMAGE` — so the `up` that
  follows really fetched them again rather than finding them cached;
- `NEOPS_MONITOR_IMAGE` is still there. It is a local-only tag that nothing can re-pull, so a
  container running elsewhere on the host has to pin it or the step cannot pass;
- no volume carries `com.docker.compose.project=<project>`. The stack keeps every byte in bind
  mounts under `data/`, which is why a volume wipe cannot touch it; the assertion is what
  would catch a named volume being introduced later.

The prune deletes every unused image, container and volume **on the whole host**, other
projects' included, and orphaned named volumes are not spared. List what will go first
(`docker volume ls -f dangling=true`) and pin anything worth keeping to a running container.
The step is off by default and refuses to run without `CHAOS_PRUNE=yes` or a typed
confirmation.

`tests/unit/test_chaos.py` holds the three `.env` corruptions against the rules that must
reject them, so a reworded message cannot turn a chaos assertion into a silent pass. It needs
no Docker.

### Keycloak

`chaos.py` runs against a password-login scenario, so the OIDC-specific breakage is driven by
hand against an `oidc-keycloak` clone. Each step ends with a fresh `keycloak_login.py` run,
which is the only thing that proves the deployment still signs people in.

| Step | Assertion |
|---|---|
| `docker kill` the `keycloak` container | it stays down, `./neops up` brings it back in under a minute and login works again |
| kill the container's `java` child | `restart: unless-stopped` recreates it on its own; PID 1 is `kc.sh`, and Docker treats a `docker kill` as a manual stop, so the policy does not fire for that |
| `./neops rotate keycloak-client` | the new secret matches in `data/secrets/keycloak-client.env`, in Keycloak and in core's `SocialApp` row, and a fresh browser login still works |
| `./neops restart cms` | `appSettings.oidcProviders` still lists Keycloak with `localLoginEnabled: false` — the seed lives in the database, not in the environment |
| create a realm role, `./neops down && ./neops up` | the role is still there and Keycloak logs `Realm 'neops' already exists. Import skipped` |

## Not covered here

- `traefik-acme` needs public DNS and port 80 reachable from the internet. The
  `compose-config` gate is its only check.
- `oidc-external` needs a real identity provider. Same.
- Single logout (`/auth/oidc-logout/`) — the login script stops at the landed session.
