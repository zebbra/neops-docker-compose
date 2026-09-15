---
title: Troubleshooting
description: What each doctor and check probe means, and fixes for the failures operators hit most.
tags: [reference, troubleshooting]
---

# Troubleshooting

Start with `./neops doctor` (health through the public URLs) and `./neops check` (host and
configuration preflight). Both print one line per probe, `OK`/`FAIL` and a detail — the sections
below are keyed to those probe names and the messages `check` prints.

## `./neops doctor` probes

| Probe | Meaning of a failure |
|---|---|
| container status (one row per service) | the container is not `running`/`healthy`, or (for the one-shot `cms-init`) did not exit 0 — `./neops logs <service>` first |
| `web client` | the web client's `/` did not return the expected marker — usually the container is still starting, or the proxy is not routing `NEOPS_WEB_URL` correctly |
| `cms admin` | `<NEOPS_CMS_URL>/admin/login/` did not return 200 — check `cms` and `cms-init` logs; see *DisallowedHost* below |
| `cms graphql` | a trivial `{__typename}` query to `/graphql` failed |
| `engine health` | `<NEOPS_ENGINE_URL>/health` did not return 200 — see *engine refuses to start* below |
| `monitor config` | `<NEOPS_WORKFLOWS_URL>/config.js` did not embed the engine URL — see *monitor blank* below |
| `keycloak realm` | the `neops` realm's OIDC discovery document did not come back — Keycloak overlay only |
| `grafana` | `/api/health` did not return 200 — metrics overlay only, and only when `NEOPS_GRAFANA_URL` is set |
| `engine worker API denied` | a `POST /blackboard/job` from outside reached the engine instead of being refused — see [External proxy](40-external-proxy.md); this never fails the overall check in external-proxy mode, since compose cannot enforce your proxy's rules, but the warning still means the route is open |
| `cms login path` | a deliberately bad login answered 5xx instead of 4xx — see *500 on login* below |
| `worker registered` | logging in as the admin user and asking the engine for `/workers` did not find an `ONLINE` worker — check the `worker` container's logs; without a permission on the admin account this probe still passes if the request itself was accepted with a 403 |
| `X-Real-IP trusted from client` (`--probe-ratelimit` only) | six forged `X-Real-IP` values were all accepted as distinct clients — your proxy is not overwriting the header, see [External proxy](40-external-proxy.md) |
| `tls certificate` (TLS scenarios only) | fewer than 14 days remain before expiry |

## `./neops check` failures

| Check | Fix |
|---|---|
| `docker daemon` | start Docker |
| `docker compose` | upgrade to Compose v2 ≥ 2.24 |
| `.env` | the message names the exact key or rule — see [Scenarios](20-scenarios.md) for the overlay and URL rules |
| `disk` | free at least 20 GiB under the data directory |
| `vm.max_map_count` | run the `sysctl -w` command the check prints, then persist it in `/etc/sysctl.d/99-neops.conf` — see *Elasticsearch max_map_count* below |
| `ports` | something else on the host already holds a port this scenario needs; stop it or change the `NEOPS_*_PORT` |
| `image` | the pinned image is neither cached locally nor pullable — run `docker login quay.io`, or the tag genuinely does not exist yet |
| `db password` | the value in `.env` no longer matches what the running Postgres accepts — it was edited by hand instead of through `./neops rotate db-password`; restore the old value or rotate properly |

## Common failures in detail

### `DisallowedHost`

Core rejects any request whose `Host` header is not in its allow-list, which the CLI derives from
`NEOPS_CMS_URL` and `NEOPS_WEB_URL` and writes to `generated/cms.env` as `DJANGO_ALLOWED_HOSTS`.
This surfaces as a CMS 400 with `DisallowedHost` in the logs (`docker compose logs cms`) when a
request arrives with a hostname the CLI does not know about — most often because a reverse proxy
in front of `compose.expose.yaml` is forwarding the wrong `Host` header, or `NEOPS_CMS_URL` /
`NEOPS_WEB_URL` were edited without running `./neops render` (which `up` and `install` already do,
but a manual `docker compose up` does not). Fix the proxy's `Host` header, or re-run
`./neops render` after an `.env` edit and restart the CMS services.

### 500 on login (`RATELIMIT_IP_META_KEY`)

Core's login rate limiter looks up the client address using the Django `META` key named by
`RATELIMIT_IP_META_KEY`. If that variable is set (the Traefik overlay sets it automatically; an
external proxy needs it uncommented in `.env` — see [External proxy](40-external-proxy.md)) but
the header it names is not actually present on the request, django-ratelimit raises and every
login attempt answers `500`. This is exactly what `doctor`'s `cms login path` probe checks. Fix:
confirm your proxy sets `X-Real-IP` on every request (not only some), or unset
`RATELIMIT_IP_META_KEY` if you are not ready to guarantee that yet.

### Engine refuses to start

Check `docker compose logs engine`. Two causes account for nearly every case:

- **`NEOPS_CMS_TOKEN` missing or a placeholder.** The engine refuses to boot with the placeholder
  token unless `NODE_ENV=development`, which this deployment never sets. `install` and `up` mint
  a real token automatically once the CMS is reachable; if the engine keeps restarting, check that
  `data/secrets/engine.env` exists and holds a real value, and that `cms` is healthy (the token can
  only be minted against a running CMS).
- **No JWT verification key.** The engine reads `NEOPS_JWT_PUBLIC_KEY_PATH`, mounted from
  `data/secrets/jwt/public.pem`. If `./neops keys` never ran (it is part of `install`/`up`), or the
  file is missing after a manual edit of `data/`, the engine refuses to start. Run `./neops keys`
  — it never overwrites an existing keypair — and restart the engine.

### Elasticsearch and `vm.max_map_count`

Elasticsearch refuses to start below `vm.max_map_count = 262144` (the stock Linux default is
65530), and dies with a message about `max virtual memory areas` in its logs
(`docker compose logs elasticsearch`) when it is too low. `./neops check` catches this before you
even try to start the stack and prints the exact fix:

```bash
sudo sysctl -w vm.max_map_count=262144
```

Persist it (the CLI's message includes this) so it survives a reboot:

```bash
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-neops.conf
```

### Monitor app blank (same origin)

The workflow manager UI disables itself entirely — rendering a blank page — when its own origin
matches the web client's origin. This is why `./neops check` refuses an `.env` where
`NEOPS_WORKFLOWS_URL` and `NEOPS_WEB_URL` share a host and port: the rule exists specifically to
catch this before the deployment starts. If you see a blank monitor page anyway, check that
`NEOPS_WORKFLOWS_URL` still resolves to something other than the web client (an `.env` edit made
outside `check`'s reach, or a proxy accidentally routing both hostnames to the same place).

### Keycloak `Invalid parameter: redirect_uri`

Keycloak rejects the login callback with this error when the redirect URI the CMS sends does not
exactly match the one registered on the `neops-auth` client — `<NEOPS_CMS_URL>/accounts/oidc/keycloak/login/callback/`
(trailing slash included). This mismatches when `NEOPS_CMS_URL` was changed *after* the realm was
imported: the import only runs on Keycloak's very first start, so a later `.env` edit does not
update the client. Fix it in the Keycloak admin console (`NEOPS_KEYCLOAK_URL`, realm `neops`,
client `neops-auth`, Valid redirect URIs) rather than editing the realm export in the repo, which
has no effect on an already-imported realm.
