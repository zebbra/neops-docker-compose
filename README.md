# neops-docker-compose

Production deployment of the Neops 2.0 stack with Docker Compose: neops-core (CMS), the workflow
engine, the workflow manager UI, a worker, the web client, Postgres per service, Redis and
Elasticsearch, with optional Traefik, Keycloak and a metrics stack.

Full documentation: `docs/` (and docs.neops.io once published).

## Prerequisites

- A Linux host with Docker Engine and Compose v2 (`docker compose version` ≥ 2.24), `git`, and
  [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- `docker login quay.io` with an account that can pull the licensed Neops images.
- DNS records for the public hostnames you choose (or one record in shared-hostname mode),
  and either a certificate for them or a public host for Let's Encrypt.
- `vm.max_map_count ≥ 262144` for Elasticsearch (`./neops check` tells you the exact command).

## Install

```bash
git clone https://github.com/zebbra/neops-docker-compose.git && cd neops-docker-compose
git checkout release/2.0
cp examples/traefik-tls-files.env .env      # pick the scenario that matches your setup
$EDITOR .env                                 # hostnames
./neops secrets                              # fills every blank secret
cp /path/to/cert.pem certs/cert.pem && cp /path/to/key.pem certs/key.pem   # tls-files only
./neops install
```

`install` validates `.env`, prepares `data/`, generates keys, renders the proxy configuration,
starts the CMS, mints the engine's API key, starts everything else and runs `./neops doctor`.
Log in at `NEOPS_WEB_URL` as `NEOPS_ADMIN_USER`.

## Scenarios

| Example | What you get |
|---|---|
| `examples/local.env` | one machine, no proxy, no TLS, `http://localhost:<port>` per service (evaluation on a laptop) |
| `examples/external-proxy.env` | services on `127.0.0.1` ports for your own reverse proxy (see `docs/40-external-proxy.md`) |
| `examples/traefik-http.env` | bundled Traefik, plain HTTP (evaluation) |
| `examples/traefik-tls-files.env` | bundled Traefik, your certificate in `certs/` |
| `examples/traefik-tls-selfsigned.env` | bundled Traefik, a self-signed certificate minted by `./neops keys` |
| `examples/traefik-acme.env` | bundled Traefik, Let's Encrypt |
| `examples/traefik-shared-host-tls-files.env` | everything on one hostname |
| `examples/oidc-external.env` | login through your identity provider |
| `examples/oidc-keycloak.env` | login through a bundled Keycloak |
| `examples/metrics.env` | + VictoriaMetrics, Grafana, exporters |

Overlays combine: edit `COMPOSE_FILE` in `.env`; `./neops check` tells you if a combination is invalid.

## Day 2

| Command | Purpose |
|---|---|
| `./neops up` | after `git pull` or an `.env` edit: migrate, render, pull, start, doctor |
| `./neops doctor` | health through the public URLs |
| `./neops status` | pinned vs running images, migrations |
| `./neops backup --keep 14` | logical dumps + `.env` + secrets + certs into `backups/<timestamp>/` |
| `./neops rotate <what>` | `db-password --which cms\|engine\|keycloak`, `admin-password`, `secret-key`, `jwt`, `tls`, `token`, `keycloak-client` |
| `./neops logs cms engine` | follow logs |
| `./neops down` | stop (data kept) |
| `./neops purge --confirm <data dir>` | delete the installation |

**Backup rule.** Everything the deployment needs is `.env` plus `data/` (plus `certs/` and
`cust-cert/`). A file copy of `data/` is only valid with the stack stopped; while it runs, use
`./neops backup`. Backups contain every secret: store them accordingly. `docker system prune -a --volumes`
loses nothing.

**Upgrades.** `git pull` (or check out the next release tag), then `./neops up`. Downgrading the CMS is
refused (`Django migrations are not reversible`); restore a backup instead.

**Secrets.** Never edit a password in `.env` by hand once installed; use `./neops rotate`. For the three
database passwords `check` notices a mismatch and names it, by connecting with the value in `.env` while
that Postgres is running; it cannot do that for a stopped service, and no other secret is checked that way.

**Local changes.** Put them in `compose.override.yaml` (gitignored) and append it to `COMPOSE_FILE`;
edited tracked files break the next `git pull`.
