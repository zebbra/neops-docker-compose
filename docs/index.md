---
title: NeOps docker-compose
description: Production docker-compose deployment of the NeOps 2.0 stack, operated with the ./neops CLI.
tags: [overview, concept]
---

# NeOps docker-compose

*A production deployment of the NeOps 2.0 stack on a single Docker host, installed and operated
with one command, `./neops`.*

This repository is a static `compose.yaml` plus a handful of overlay files, selected in `.env`
through `COMPOSE_FILE`. A small Python package, `neops_compose`, validates the configuration,
renders the few files that depend on it, generates key material, runs numbered deployment
migrations, and drives the two-phase first start the stack needs. Everything durable lives under
`data/` as a bind mount, so the whole deployment can be rebuilt from `.env` plus `data/`.

See [Install](10-install.md) to bring up a deployment, [Scenarios](20-scenarios.md) for the
overlay model and the routing and TLS choices, [Operations](30-operations.md) for day-2 commands,
[External proxy](40-external-proxy.md) if you are not using the bundled Traefik, and
[Troubleshooting](50-troubleshooting.md) when something does not come up healthy.

## Services

| Service | Image | Role |
|---|---|---|
| `postgres-cms` | `postgres:16-alpine` | database for the CMS (`neops`/`neops`) |
| `postgres-engine` | `postgres:16-alpine` | database for the workflow engine (`postgres`/`neops-workflow`) |
| `postgres-keycloak` | `postgres:16-alpine` | database for Keycloak — keycloak overlay only |
| `redis` | `redis:7-alpine` | Celery broker, Django cache, channel layer; ephemeral by design |
| `elasticsearch` | `elasticsearch:8.9.2` | search index behind the CMS device/interface views |
| `cms-init` | `neops-core` | one-shot: Django migrations, create the Elasticsearch indices, create the superuser |
| `cms` | `neops-core` | the CMS web process (network CMS + GraphQL API at `/graphql`) |
| `cms-worker` | `neops-core` | Celery worker |
| `cms-beat` | `neops-core` | Celery beat scheduler |
| `engine` | `neops-workflow-engine` | the 2.0 workflow engine (blackboard REST API) |
| `monitor` | `neops-monitor-app` | the workflow manager UI (temporary, being folded into the web client) |
| `worker` | `neops-worker-sdk` | polls the engine's blackboard and drives devices; base function blocks only |
| `web` | `neops-web-client` | the production Angular web client |
| `keycloak` | `keycloak:26.5.2` | bundled identity provider — keycloak overlay only |
| `traefik` | `traefik:v3.6.25` | bundled reverse proxy and TLS termination — traefik overlay only |
| VictoriaMetrics, vmalert, Grafana, exporters | — | metrics overlay only |

Every service shares one Docker network; the base `compose.yaml` publishes no ports. Which ports
reach the host depends on the routing overlay you pick — see [Scenarios](20-scenarios.md).

## Durable state

Every byte the deployment needs to survive lives under `${NEOPS_DATA_DIR:-./data}` as a bind
mount. No compose file declares a named volume.

```
data/
├── cms/postgres/          Postgres data directory for postgres-cms
├── cms/media/             Django MEDIA_ROOT
├── cms/tmp/                core scratch space (report generation)
├── engine/postgres/        Postgres data directory for postgres-engine
├── keycloak/postgres/      Postgres data directory for postgres-keycloak      [keycloak overlay]
├── elasticsearch/          Elasticsearch data, owned by uid 1000
├── traefik/acme/            acme.json                                        [tls-acme overlay]
├── metrics/victoria/       metrics/grafana/                                  [metrics overlay]
├── secrets/                mode 0700; every file inside is 0600
│   ├── jwt/private.pem  jwt/public.pem     the CMS signs, the engine verifies
│   ├── engine.env                          the engine's CMS API key
│   ├── keycloak-client.env                 the Keycloak client secret        [keycloak overlay]
│   └── tls/cert.pem  tls/key.pem  tls/ca.pem   self-signed certificate       [NEOPS_TLS_SELF_SIGNED=true]
└── .neops/state.json       applied deployment migrations, install metadata
```

`generated/` (also gitignored, also 0700) holds files the CLI renders from `.env` on every
`./neops render`: the Traefik static and dynamic configuration, per-service env files that carry
values Compose cannot interpolate, the OIDC provider seed, and the Keycloak realm import. Nothing
under `generated/` is hand-edited; a stopped-and-restarted `./neops up` rebuilds it from `.env`.

Redis is the only stateful service with no directory of its own, on purpose: it holds Celery's
work queue and Django's cache, and losing it on a wipe costs at most the tasks in flight.
Elasticsearch is derived data; it is not part of `./neops backup` and is rebuilt with
`manage.py elastic_index --create --populate` after a restore.
