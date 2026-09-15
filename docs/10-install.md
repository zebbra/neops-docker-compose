---
title: Install
description: Prerequisites and the step-by-step install walk-through.
tags: [howto]
---

# Install

## Prerequisites

- A Linux host with Docker Engine and Compose v2 (`docker compose version` reports at least 2.24),
  `git`, and [`uv`](https://docs.astral.sh/uv/).
- `docker login quay.io` with an account that can pull the licensed NeOps images.
- DNS records for the public hostnames the scenario you pick needs (one record in
  shared-hostname mode, up to six otherwise — see [Scenarios](20-scenarios.md)).
- A TLS certificate for those hostnames, or a publicly reachable host on port 80 for Let's
  Encrypt, or none at all if you generate a self-signed certificate or run behind your own proxy.
- `vm.max_map_count` at or above `262144`, Elasticsearch's own minimum. `./neops check` reports
  the current value and the exact `sysctl` line to fix it.
- At least 20 GiB free under the data directory. `./neops check` refuses to proceed below that.

## Choose a scenario

Copy the `examples/*.env` file that matches your setup to `.env`:

```bash
cp examples/traefik-tls-files.env .env
```

The [scenario table](20-scenarios.md) explains every option. Open `.env` and fill in:

- the four (or six, with Keycloak and metrics) public URLs, for your own hostnames;
- every blank secret — generate each with `openssl rand -hex 32`;
- `NEOPS_TLS_CERT_FILE` / `NEOPS_TLS_KEY_FILE` (or copy your certificate into `certs/cert.pem`
  and `certs/key.pem`), unless you are using `NEOPS_TLS_SELF_SIGNED=true` or Let's Encrypt.

`.env.example` documents every key the CLI understands, with its default and which overlay reads
it.

## Install

```bash
./neops install
```

`install` does the following, in order, and stops at the first failure with an actionable
message:

1. **`check`** — the same preflight `./neops check` runs on its own: Docker and Compose versions,
   `.env` and scenario validity, disk space, `vm.max_map_count`, every pinned image resolvable on
   Quay, free host ports.
2. **`migrate`** — applies any pending deployment migration. On a first install this creates the
   `data/` tree (including `secrets/` at mode 0700 and `elasticsearch/` owned by uid 1000) and the
   state file.
3. **`keys`** — generates the JWT keypair the CMS and the engine share, the Keycloak client secret
   (keycloak overlay), and a self-signed certificate (only when `NEOPS_TLS_SELF_SIGNED=true`).
   Existing key material is never overwritten; rotation only happens through `./neops rotate`.
4. **`render`** — writes `generated/`: the Traefik configuration, the per-service env files, the
   OIDC provider seed and the Keycloak realm import, all derived from `.env`.
5. **Pull every image**, then **start the CMS first** (`postgres-cms`, `redis`, `elasticsearch`,
   `cms-init`, `cms`) and wait for it to become healthy. `cms-init` runs the CMS's own Django
   migrations, creates the Elasticsearch indices, and creates the first superuser from
   `NEOPS_ADMIN_USER` / `NEOPS_ADMIN_EMAIL` / `NEOPS_ADMIN_PASSWORD`.
6. **Mint the engine's API key** against the now-running CMS (`manage.py generate_api_key`), and
   write it to `data/secrets/engine.env`. This is why the CMS has to be up first: the engine
   refuses to boot without a valid token.
7. **Start everything else** — the engine, the monitor, the worker, the web client, and any
   overlay services (Traefik, Keycloak, the metrics stack).
8. **`doctor`** — a health report through the public URLs. `install` exits non-zero if anything
   fails here, even though every container may already be running.

Running `install` again on an already-installed deployment changes nothing: every step is
idempotent, and the second run reports the existing state rather than erroring. Use `./neops up`
for that case instead (see [Operations](30-operations.md)); it is the command for "the repo or
`.env` changed, bring the deployment in line."

## First login

Once `doctor` reports every probe `OK`, open `NEOPS_WEB_URL` in a browser and log in as
`NEOPS_ADMIN_USER` with the password from `.env`. That password is applied only when the account
is created; change it afterwards with `./neops rotate admin-password`, not by editing `.env`.

## Adding users

Without an OIDC overlay, create additional accounts and grant permissions from the CMS admin
site at `<NEOPS_CMS_URL>/admin/`, signed in as the superuser. With `compose.oidc.yaml`, local
password login is disabled: users authenticate through your identity provider, and with the
bundled Keycloak overlay you create them in the Keycloak admin console and assign them roles on
the `neops-auth` client (see [Scenarios](20-scenarios.md#oidc)). Either way, the very first
account able to manage the deployment is the superuser `install` created.
