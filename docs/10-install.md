---
title: Install
description: Prerequisites and the step-by-step install walk-through.
tags: [howto]
---

# Install

## Prerequisites

- A Linux host with Docker Engine and Compose v2 (`docker compose version` reports at least 2.24),
  `git`, and [`uv`](https://docs.astral.sh/uv/).
- `docker login quay.io` with an account that can pull the licensed Neops images.
- DNS records for the public hostnames the scenario you pick needs (one record in
  shared-hostname mode, up to six otherwise, see [Scenarios](20-scenarios.md)).
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
- every blank secret: `./neops secrets` generates the ones your scenario needs and leaves
  anything already set alone (or generate each by hand with `openssl rand -hex 32`);
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

1. **`check`**: the same preflight `./neops check` runs on its own, minus the images: Docker and
   Compose versions, `.env` and scenario validity, disk space, `vm.max_map_count`, free host ports.
2. **`migrate`**: applies any pending deployment migration. On a first install this creates the
   `data/` tree (including `secrets/` at mode 0700 and `elasticsearch/` owned by uid 1000) and the
   state file.
3. **`keys`**: generates the JWT keypair the CMS and the engine share, the Keycloak client secret
   (keycloak overlay), and a self-signed certificate (only when `NEOPS_TLS_SELF_SIGNED=true`).
   Existing key material is never overwritten; rotation only happens through `./neops rotate`.
4. **`render`**: writes `generated/`: the Traefik configuration, the per-service env files, the
   OIDC provider seed and the Keycloak realm import, all derived from `.env`.
5. **Check every pinned image**: each one is either already on this host or resolvable in the
   registry. This comes after `render` rather than with the rest of the preflight because
   `docker compose` cannot resolve the image list until `generated/` exists, which is also why
   `./neops check --no-images` is what you run before a first install.
6. **Pull every image**, then **start the CMS first** (`postgres-cms`, `redis`, `elasticsearch`,
   `cms-init`, `cms`) and wait for it to become healthy. `cms-init` runs the CMS's own Django
   migrations, creates the Elasticsearch indices, creates the first superuser from
   `NEOPS_ADMIN_USER` / `NEOPS_ADMIN_EMAIL` / `NEOPS_ADMIN_PASSWORD`, and grants that superuser
   the role described under [The first user's permissions](#the-first-users-permissions).
7. **Mint the engine's API key** against the now-running CMS (`manage.py generate_api_key`), and
   write it to `data/secrets/engine.env`. This is why the CMS has to be up first: the engine
   refuses to boot without a valid token.
8. **Start everything else**: the engine, the monitor, the worker, the web client, and any
   overlay services (Traefik, Keycloak, the metrics stack).
9. **`doctor`**: a health report through the public URLs. `install` exits non-zero if anything
   fails here, even though every container may already be running.

`./neops up` repeats every step except the image check: resolving images is `check`'s job, and
repeating it on each start would make every restart depend on the registry. Run `./neops check`
after a `git pull` that moves an image tag. `up` on a deployment that was never installed takes
the same two-phase start as `install`, so starting with `up` is not a mistake, only a skipped
image check.

!!! note "external-proxy mode ends on a `WARN`"

    With `compose.expose.yaml` the last step reports
    `WARN engine worker API denied`. That probe asks the engine's public URL to answer a worker
    route and expects your reverse proxy to refuse it — which it cannot do before you have
    configured and started it. The install finishes and exits 0. Once the proxy is routing, run
    `./neops doctor` again and the probe should turn `OK`; if it stays a warning, your proxy is
    not denying those routes. [External proxy](40-external-proxy.md) has the snippets. Behind the
    bundled Traefik the same probe is a hard failure, because there the deny rule is one the CLI
    rendered itself. With `examples/local.env` there is no proxy at all, so the warning is
    permanent: it is the reminder that the deployment must stay on `127.0.0.1`.

Running `install` again on an already-installed deployment changes nothing: every step is
idempotent, and the second run reports the existing state rather than erroring. Use `./neops up`
for that case instead (see [Operations](30-operations.md)); it is the command for "the repo or
`.env` changed, bring the deployment in line."

## First login

Once `doctor` reports every probe `OK`, open `NEOPS_WEB_URL` in a browser and log in as
`NEOPS_ADMIN_USER` with the password from `.env`. That password is applied only when the account
is created; change it afterwards with `./neops rotate admin-password`, not by editing `.env`.

## The first user's permissions

Being a Django superuser grants nothing in Neops itself. Core gates every entity read and write
on a *role*, so an account without one logs in, sees empty tables and gets
`User is not allowed to create a group.` from every write. `cms-init` therefore also seeds, for
`NEOPS_ADMIN_USER`:

- a role named by `NEOPS_ADMIN_ROLE` (default `admin`) holding read, execute and write;
- a scope named `Global` with all five visibility flags on (devices, groups, interfaces,
  clients, topology), and that role granted read, execute and write on it;
- the `admin` workflow profile on the same role, which adds the workflow, execution and worker
  grants (`manage.py grant_workflow_permissions --role <role> --profile admin`).

Every step is idempotent and re-runs on each `./neops up`. It never revokes anything, so widening
or narrowing that role afterwards in the admin site is safe for the permissions it does not
mention, but the three grants above are re-applied on every start. Renaming `NEOPS_ADMIN_ROLE`
seeds a *second* role rather than renaming the first.

## Adding users

Without an OIDC overlay, create additional accounts from the CMS admin site at
`<NEOPS_CMS_URL>/admin/`, signed in as the superuser. A new account needs the same two things the
first one was given, both under **Permissions** in the admin site: a role (with its permission
level and a scope granting the visibility you want), and that role on the user. Workflow rights
are separate, and are added per role with

```bash
./neops compose -- exec cms python manage.py grant_workflow_permissions --role <role> \
  --profile author|operator|admin --yes
```

Without `--yes` the command prints the diff and changes nothing, which is the safe way to see
what a profile would add. It is additive: a profile widens a role and never revokes a grant.

With `compose.oidc.yaml`, local password login is disabled: users authenticate through your
identity provider, and with the bundled Keycloak overlay you create them in the Keycloak admin
console and assign them roles on the `neops-auth` client (see [Scenarios](20-scenarios.md#oidc)).
The role names that arrive in the token still have to exist in the CMS with a scope and
permissions, exactly as above. Either way, the very first account able to manage the deployment
is the superuser `install` created.
