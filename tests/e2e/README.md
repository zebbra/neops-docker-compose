# End-to-end scenario runs

`run_scenario.py` installs one `examples/*.env` scenario for real — containers, images,
databases — in a throwaway clone of this checkout, asserts the deployment works, and brings
it down again. It is the only test in this repo that touches Docker; `make check` does not
run it.

```bash
make e2e SCENARIO=traefik-tls-selfsigned
uv run python tests/e2e/run_scenario.py external-proxy --port-base 18000
uv run python tests/e2e/run_scenario.py traefik-tls-selfsigned --keep --workdir /tmp/neops-e2e/tls
```

A run takes 5 to 10 minutes once the images are cached; the first one pulls about 3 GB.
`cms-init` on a cold Elasticsearch takes roughly two minutes and the CMS healthcheck has a
150 s start period, so a stack that looks stuck usually is not. Watch it with
`./neops compose -- logs -f` inside the clone printed as `workdir ...`.

## What it asserts

| Assertion | Scenarios |
|---|---|
| `install` completes and doctor is green | all except expose mode |
| the worker API deny probe is doctor's **only** failure | expose mode (`compose.expose.yaml`) |
| the admin user can log in and gets an access token | password-login scenarios |
| the Django admin works over plain http: no HSTS, neither cookie is `Secure`, and the login form POST is accepted | `http://` scenarios |
| the admin creates a device group over GraphQL, reads it back and deletes it, which is what the seeded admin role buys | password-login scenarios |
| the OIDC providers are seeded and visible in `appSettings` | OIDC scenarios |
| core's admin, `/djstatic/` and the engine's `/engine/health` answer on the one hostname | shared-host |
| the worker container runs and its log shows function blocks registering | all |
| `cms-worker` and `cms-beat` run and are healthy under `COMPOSE_PROFILES=cms-tasks`, and do not exist without it | all |
| a device created by hand on the seeded `Linux Generic` platform, a `generic-static-facts` task executed on it over GraphQL, run by `cms-worker` to `SUCCESSFUL`, and the fact read back from the device | `cms-tasks` profile (`local-legacy`) |
| every exporter, VictoriaMetrics, vmalert and Grafana run; Grafana answers `/api/health` on its public URL; every scrape target reports `up` | metrics |
| `./neops backup` writes `cms.dump` and `engine.dump` | all |
| a second `install` mints no second API key | all |

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

`chaos.py` takes the clone a `--keep` run left behind and breaks the deployment on purpose.

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
