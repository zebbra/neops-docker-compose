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
- Free space for Elasticsearch: it refuses every shard while the filesystem is above its high
  watermark, which on a default install is 10% of the whole disk. `./neops check` prints the
  figure; `NEOPS_ES_HEADROOM` caps it on a large shared filesystem (see `examples/local.env`).

## Get the deployment

Clone the repository's `develop` branch onto the host, and run every later command from that
directory:

```bash
git clone --branch develop https://github.com/zebbra/neops-docker-compose.git
cd neops-docker-compose
```

`develop` carries the current Neops 2.0 release, and an [upgrade](30-operations.md#upgrades) is
a `git pull` on it. `main` holds the old pre-2.0 tree, and the Neops 1.0 files are on
`release/1.0`: install from neither.

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
   fails here, even though every container may already be running. A certificate this host does
   not trust fails here too: see
   [When this host does not trust the certificate](#when-this-host-does-not-trust-the-certificate).

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
    rendered itself. With `examples/local.env` there is no proxy at all and no network path
    either, engine URL and bind address both on loopback, so the probe reports `skipped` and
    stays `OK`.

Running `install` again on an already-installed deployment changes nothing: every step is
idempotent, and the second run reports the existing state rather than erroring. Use `./neops up`
for that case instead (see [Operations](30-operations.md)); it is the command for "the repo or
`.env` changed, bring the deployment in line."

## When this host does not trust the certificate

`doctor` reaches every public URL over HTTPS and verifies the certificate chain and hostname
against the default trust store of the Python that `./neops` runs. When this host does not trust
the certificate, every HTTPS probe fails with `CERTIFICATE_VERIFY_FAILED`, and `install` and `up`
end with `error: the stack is up but doctor reports failures` and exit 1. Every step before
`doctor` has run and the containers are up. This happens with:

- the self-signed certificate (`NEOPS_TLS_SELF_SIGNED=true`): its private CA,
  `data/secrets/tls/ca.pem`, is trusted nowhere until you add it;
- a certificate from an internal or enterprise CA that this host's trust store does not carry.

Pick one of the fixes below, then run the same command again; running `install` twice is safe
(see above). Until an `install` or `up` passes `doctor`, `./neops status` reports `doctor FAILED`;
a `./neops doctor` run on its own does not change that.

**Point `SSL_CERT_FILE` at a bundle that holds your CA.** Python reads that file *instead of* its
default bundle, so concatenate the system bundle and your CA rather than naming the CA alone. In
self-signed mode the CA is `data/secrets/tls/ca.pem`.

```bash
# Debian/Ubuntu; on RHEL the system bundle is /etc/pki/tls/certs/ca-bundle.crt
cat /etc/ssl/certs/ca-certificates.crt /path/to/your-ca.pem > ~/neops-ca-bundle.pem
export SSL_CERT_FILE=~/neops-ca-bundle.pem
./neops install
```

Every later `./neops up` and `./neops doctor` needs the variable too, so put the `export` in the
operator's shell profile.

**Or add the CA to this host's trust store** (`update-ca-certificates` on Debian/Ubuntu,
`update-ca-trust` on RHEL). This works only if that store is where the CLI's Python looks:
`uv run python -c "import ssl; print(ssl.get_default_verify_paths())"`, run in this directory,
prints the paths. If `doctor` still fails afterwards, use `SSL_CERT_FILE`.

**Or pass `--insecure`.** `install`, `up` and `doctor` accept it, and it changes `doctor` only:
every HTTPS probe skips verifying the certificate chain and the hostname. Every other step runs
as without it, and the `tls certificate` probe still reads the certificate and still fails when
14 days or fewer remain. What you give up is `doctor`'s proof that a client can trust the
certificate for your hostnames; browsers and every other client still have to
trust it themselves. Pass the flag to every later `up` as well, or that `up` exits 1 the same
way.

!!! note "A trusted CA that still fails"

    From Python 3.13 on, verification is strict about RFC 5280, so a CA certificate the host does
    trust can still be refused, for instance with
    `Path length given without key usage keyCertSign` (the CA sets a path length but has no key
    usage extension). Have your CA reissued, or use `--insecure`. A self-signed CA minted by an
    older `./neops keys` has exactly that defect: `./neops rotate tls` mints a new CA and
    certificate that verify, and every browser that imported the old `data/secrets/tls/ca.pem`
    needs the new one.

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
- that role granted read, execute and write on the scope `Global`, which core creates with all
  five visibility flags on (devices, groups, interfaces, clients, topology); the seed leaves the
  scope's own settings as they are;
- write on the `network-zones` permission element, so the admin can create
  [network zones](35-network-zones.md);
- the `admin` workflow profile on the same role, which adds the workflow, execution and worker
  grants (`manage.py grant_workflow_permissions --role <role> --profile admin`).

The first three come from core's `manage.py bootstrap_admin_role --user <user> --role <role>`.
Every step is idempotent and re-runs on each `./neops up`. It never revokes anything, so widening
or narrowing that role afterwards in the admin site is safe for the permissions it does not
mention, but the four grants above are re-applied on every start: one removed from that role
comes back. Renaming `NEOPS_ADMIN_ROLE` seeds a *second* role rather than renaming the first.
If other users already hold a role of this name, the seed widens it only by the `network-zones`
write and warns, in `./neops logs cms-init` only: it adds `NEOPS_ADMIN_USER` to that role, but
skips the scope grant and the workflow profile. With an OIDC overlay that is the usual case, since
every user your identity provider gives the `admin` client role holds the role, and each of them
then may write every zone (zones are not per tenant). Set `NEOPS_ADMIN_ROLE` to a role only the
superuser holds; the seed re-applies the write on every `up`, so remove it from the shared role by
hand afterwards if it should not have it.

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
