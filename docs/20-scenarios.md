---
title: Scenarios
description: The overlay model, routing modes, TLS modes, OIDC and the metrics overlay.
tags: [concept, reference]
---

# Scenarios

A deployment is `compose.yaml` plus the overlay files named in `COMPOSE_FILE`. `.env` selects
them; nothing else about the compose files changes between deployments.

```
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=compose.yaml:compose.traefik.yaml:compose.tls-files.yaml
```

| Overlay | File | Adds |
|---|---|---|
| external proxy | `compose.expose.yaml` | publishes each service on `127.0.0.1:<port>` for a reverse proxy you run |
| Traefik | `compose.traefik.yaml` | a bundled Traefik that terminates TLS and routes by hostname |
| shared hostname | `compose.traefik-shared-host.yaml` | routes the CMS and the monitor on the web client's own hostname instead of separate ones (needs Traefik) |
| TLS from files | `compose.tls-files.yaml` | your certificate, or a self-signed one minted by `./neops keys` (needs Traefik) |
| TLS from Let's Encrypt | `compose.tls-acme.yaml` | HTTP-01 certificates (needs Traefik) |
| OIDC | `compose.oidc.yaml` | login through an external identity provider; disables local password login |
| Keycloak | `compose.keycloak.yaml` | a bundled Keycloak as that identity provider (needs OIDC) |
| metrics | `compose.metrics.yaml` | VictoriaMetrics, vmalert, Grafana, and per-service exporters |

The Celery worker and beat containers of core's 1.0 task path are not part of any scenario by
default: a 2.0 deployment runs its automation through the engine and the worker SDK, and the CMS
indexes Elasticsearch synchronously without them. A deployment that still runs 1.0 tasks or
cron jobs adds `COMPOSE_PROFILES=cms-tasks` to `.env`, which starts both with the next
`./neops up`; removing the line and running `./neops down` then `./neops up` stops them again.

Overlays combine freely, and `examples/` ships one complete `.env` per combination we test. Put
anything of your own in `compose.override.yaml` (gitignored) and append it to `COMPOSE_FILE`;
never edit a tracked compose file, or the next `git pull` conflicts with your change.

## `./neops check` and invalid combinations

`check` (also the first step of `install` and `up`) validates the scenario before anything
starts, and names the exact problem rather than failing mid-deployment:

- `COMPOSE_FILE` must start with `compose.yaml`, and every file it names must exist and be a
  shipped overlay (a local file belongs in `compose.override.yaml` instead).
- Exactly one of the external-proxy or Traefik overlay must be present.
- At most one of the two TLS overlays, and either one requires Traefik.
- The shared-hostname overlay requires Traefik.
- The Keycloak overlay requires the OIDC overlay.
- `compose.tls-acme.yaml` requires `NEOPS_HTTP_PORT=80`: the HTTP-01 challenge has no other port.
- Every required key for the overlays you selected is set, non-blank, long enough (16 characters
  for a password) and not a placeholder like `changeme`.

## Routing modes

`.env` declares the browser-facing URL of every entry point; the CLI derives Traefik's
configuration and the CMS's allowed-hosts and CORS settings from them. Two routing shapes.

### Hostname per service (default)

```
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://cms.neops.example.com
NEOPS_ENGINE_URL=https://engine.neops.example.com
NEOPS_WORKFLOWS_URL=https://workflows.neops.example.com
NEOPS_KEYCLOAK_URL=https://auth.neops.example.com      # keycloak overlay only
NEOPS_GRAFANA_URL=https://grafana.neops.example.com    # metrics overlay only
```

Every URL is its own origin, all on the same port (`NEOPS_HTTPS_PORT`, or `NEOPS_HTTP_PORT`
without TLS). No public URL carries a path unless the shared-hostname overlay is in
`COMPOSE_FILE`: a path only makes sense once you route by prefix, which is what that overlay is
for. `NEOPS_CMS_URL` is the one exception that goes further: it never carries a path in either
mode, because core cannot be served under a prefix at all (see below).

### Port per service, no proxy (local)

```
COMPOSE_FILE=compose.yaml:compose.expose.yaml
NEOPS_WEB_URL=http://localhost:8080
NEOPS_CMS_URL=http://localhost:8000
NEOPS_ENGINE_URL=http://localhost:3030
NEOPS_WORKFLOWS_URL=http://localhost:3031
```

`examples/local.env` is the external-proxy overlay with nobody in front of it: the browser talks
to each `127.0.0.1` port directly, over plain HTTP, with local password login. Each port is its
own origin, so the same-origin rules above are satisfied without any hostname. It is the
compose equivalent of the neops-lab control plane and is meant for one machine: with no proxy
to deny them, the engine's worker routes answer to every process on this host, which is why
`NEOPS_BIND_ADDRESS` must stay `127.0.0.1`: with both the engine URL and the bind address on
loopback, `./neops doctor` skips the deny probe rather than warning forever, and the moment
either leaves loopback the warning is back. Put a proxy in front and switch to
`examples/external-proxy.env` before exposing it to a network.

### Shared hostname

```
NEOPS_WEB_URL=https://neops.example.com
NEOPS_CMS_URL=https://neops.example.com                # same origin as the web client, no path
NEOPS_ENGINE_URL=https://neops.example.com/engine
NEOPS_WORKFLOWS_URL=https://neops.example.com:8443     # distinct origin; port = NEOPS_MONITOR_PORT
NEOPS_KEYCLOAK_URL=https://neops.example.com/sso       # any path that is not a core or web-client prefix
NEOPS_GRAFANA_URL=https://neops.example.com/grafana
```

One certificate, one DNS record. `NEOPS_CMS_URL` must equal `NEOPS_WEB_URL` exactly: core cannot
be served under a path prefix (its static file URLs are absolute), so instead the CMS's own URL
prefixes (`/graphql`, `/graphiql`, `/admin`, `/djstatic`, `/.well-known`, `/accounts`, the
`/auth/oidc-*` routes and `/webhook`) are routed to it on the web client's hostname, unstripped.
That list lives in `neops_compose/routes.py`; a core release that adds a new top-level URL prefix
needs that file updated too. `NEOPS_WORKFLOWS_URL` (the monitor app) always needs a distinct
origin from the web client. It is served on its own port, `NEOPS_MONITOR_PORT`, because the web
client itself disables the workflow-manager link when the two origins match.

Whichever mode you pick, `NEOPS_WORKFLOWS_URL` must never be the same origin as `NEOPS_WEB_URL`,
and paths reserved by the web client's own SPA (`/auth`, `/login`) can't be reused by another
service either.

## TLS

Three modes, all requiring the Traefik overlay:

| Mode | `.env` | Notes |
|---|---|---|
| Your certificate | `compose.tls-files.yaml`, `NEOPS_TLS_CERT_FILE` / `NEOPS_TLS_KEY_FILE` | a SAN or wildcard certificate covering every hostname in use, dropped into `certs/` |
| Self-signed | `compose.tls-files.yaml`, `NEOPS_TLS_SELF_SIGNED=true` | `./neops keys` mints a private CA and a server certificate under `data/secrets/tls/`, with a SAN for every hostname your URLs name; import `data/secrets/tls/ca.pem` into your browser, or accept the warning |
| Let's Encrypt | `compose.tls-acme.yaml`, `NEOPS_ACME_EMAIL` | HTTP-01, so `NEOPS_HTTP_PORT` must be 80 and reachable from the internet on every hostname; certificates land in `data/traefik/acme/acme.json` |
| None (evaluation) | `compose.traefik.yaml` alone | plain HTTP on `NEOPS_HTTP_PORT` |

The self-signed certificate carries a SAN for every hostname derived from your URLs. If you add a
hostname later (say, turning on the metrics overlay's `NEOPS_GRAFANA_URL`) and the existing
certificate does not cover it, `./neops up` refuses to proceed until you run `./neops rotate tls`.

## OIDC

`compose.oidc.yaml` switches the CMS's auth plugin to `neops_auth_allauth` and disables local
password login. Two ways to provide the identity provider:

### External IdP

```
NEOPS_OIDC_PROVIDER_ID=entra          # short id; becomes part of the callback URL
NEOPS_OIDC_NAME=Company SSO           # label on the login button
NEOPS_OIDC_CLIENT_ID=
NEOPS_OIDC_CLIENT_SECRET=
NEOPS_OIDC_DISCOVERY_URL=https://login.example.com/.well-known/openid-configuration
```

Register this redirect URI at the provider:

```
<NEOPS_CMS_URL>/accounts/oidc/<NEOPS_OIDC_PROVIDER_ID>/login/callback/
```

Core reads roles out of the token's `resource_access.<client_id>.roles` claim, so the
provider's client needs to put that claim on the ID token or userinfo response, exactly what the
bundled Keycloak overlay configures automatically (see below). Without it, users can log in but
land with no permissions until one is assigned by hand.

Core also refuses any login whose token does not say `email_verified: true`, because a matching
email links the OIDC identity to an existing local user. A provider that never emits that claim
needs the opt-out, which means trusting the provider's email as it is:

```
NEOPS_OIDC_TRUST_EMAIL_WITHOUT_VERIFICATION=true
```

#### Microsoft Entra ID (Azure AD)

Register a web app under *Microsoft Entra ID, App registrations* with:

- Platform *Web*, redirect URIs `<NEOPS_CMS_URL>/accounts/oidc/<NEOPS_OIDC_PROVIDER_ID>/login/callback/`
  (trailing slash included) and `<NEOPS_WEB_URL>/login`. The second one is where core sends the
  browser after an Entra sign-out, and Entra only accepts a post-logout URL that is also a
  registered redirect URI.
- A client secret under *Certificates and secrets*; `NEOPS_OIDC_CLIENT_SECRET` is its value, not
  its ID, and it expires.
- The optional claim `email` on the ID token under *Token configuration*; core derives the
  username from it and links accounts by it.

Then in `.env`: `NEOPS_OIDC_CLIENT_ID` is the *Application (client) ID*, the discovery URL is
`https://login.microsoftonline.com/<Directory (tenant) ID>/v2.0/.well-known/openid-configuration`
(the tenant ID rather than `common`, so only your tenant signs in), and
`NEOPS_OIDC_TRUST_EMAIL_WITHOUT_VERIFICATION=true`, because Entra does not emit `email_verified`.
Entra puts app roles in a top-level `roles` claim that core does not read yet, so roles are
assigned in Neops by hand until core learns that claim shape.

### Bundled Keycloak

```
COMPOSE_FILE=...:compose.oidc.yaml:compose.keycloak.yaml
NEOPS_KEYCLOAK_URL=https://auth.neops.example.com
NEOPS_KEYCLOAK_ADMIN_PASSWORD=
NEOPS_KEYCLOAK_DB_PASSWORD=
```

On first start, Keycloak imports a realm named `neops` with a confidential client `neops-auth`,
whose redirect URI is already set to `<NEOPS_CMS_URL>/accounts/oidc/keycloak/login/callback/` and
whose post-logout redirect is `<NEOPS_WEB_URL>/*`. The client carries a client-roles mapper that
puts `resource_access.neops-auth.roles` into the ID token and userinfo: the claim the web client
needs, already wired up. The client secret is generated by `./neops keys` and stored in
`data/secrets/keycloak-client.env`; `providers.json` (the CMS's provider seed) is rendered to
point at it automatically.

The realm import runs only on the very first start. To add users or change client settings after
that, use the Keycloak admin console at `NEOPS_KEYCLOAK_URL` (user `admin`, password
`NEOPS_KEYCLOAK_ADMIN_PASSWORD`): create a user, then assign it roles on the `neops-auth`
client. Editing the realm export in the repo has no effect on a running deployment.

## Metrics

`compose.metrics.yaml` adds VictoriaMetrics (30-day retention, `data/metrics/victoria`), vmalert
with the rule set in `metrics/vmalert/rules`, per-service exporters (`redis`, `postgres-cms`,
`postgres-engine`, Celery, Elasticsearch), and Grafana (`data/metrics/grafana`, provisioning from
`metrics/grafana`). Grafana is always published on `127.0.0.1:${NEOPS_GRAFANA_PORT:-3000}` (or
`NEOPS_BIND_ADDRESS`), in every scenario, the same way Keycloak's admin port always is. It is
routed publicly on top of that, through Traefik, only when `NEOPS_GRAFANA_URL` is set. Leave
`NEOPS_GRAFANA_URL` unset to keep Grafana loopback-only. Set `NEOPS_GRAFANA_ADMIN_PASSWORD` either
way; Grafana needs it to start.
