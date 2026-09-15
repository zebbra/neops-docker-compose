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
| the OIDC providers are seeded and visible in `appSettings` | OIDC scenarios |
| core's admin, `/djstatic/` and the engine's `/engine/health` answer on the one hostname | shared-host |
| the worker container runs and its log shows function blocks registering | all |
| `./neops backup` writes `cms.dump` and `engine.dump` | all |
| a second `install` mints no second API key | all |

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
| base + 444 | the monitor entrypoint in shared-host mode |
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

`quay.io/zebbra/neops-monitor-app` is not published yet, so the harness sets
`NEOPS_MONITOR_IMAGE=neops-monitor-app:local` and expects that image to exist on the host.
Build it from the workflow-engine checkout (`rest/monitor-app/Dockerfile`) if it does not.
`docker compose pull` cannot fetch a local-only tag, which is why `Compose.pull()` passes
`--ignore-pull-failures`.

## The override file

The harness writes a `compose.override.yaml` into the clone and appends it to `COMPOSE_FILE`.
It disables Elasticsearch's disk allocation thresholds, because a host above the 95% flood
stage turns every index read-only and `cms-init` then fails in a way that has nothing to do
with the change under test. The file is gitignored and is exactly what it is there for.

## Not covered here

- `traefik-acme` needs public DNS and port 80 reachable from the internet. The
  `compose-config` gate is its only check.
- `oidc-external` needs a real identity provider. Same.
- The Keycloak browser login is a manual step: run `oidc-keycloak` with `--keep`, create a
  user in the admin console at `https://auth.neops.localhost:<base+443>` (user `admin`,
  password `NEOPS_KEYCLOAK_ADMIN_PASSWORD` from the clone's `.env`), assign it client roles
  on `neops-auth`, then open the web client and sign in through the Keycloak button.
