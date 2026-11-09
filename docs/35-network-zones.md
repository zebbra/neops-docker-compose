---
title: Network zones
description: Running workers per network zone on a compose install, creating the first zone, and retiring a site.
tags: [howto, operations]
---

# Network zones

A network zone is a network that only its own workers reach, so only a worker that serves a
job's zone can claim it; a device nobody assigned is in `default`. The workflow engine's
[Network Zones][engine-zones] page owns the rules; this page covers what is particular to a
compose install. Zones need neops-core 2.4, workflow engine 0.46 and worker 0.5 or later; before
upgrading to them, republish the workflows that pin a
[re-versioned base block](30-operations.md#workflows-that-pin-a-re-versioned-base-block).

A worker serves the zones listed in `NEOPS_WORKER_NETWORK_ZONES`, comma-separated. Once the CMS
holds any zone, the engine refuses a worker that lists none: it exits at startup and asks for the
setting. That protects against a forgotten setting, not against a host at the site: a remote
site's worker that lost its setting would otherwise reach the default network's addresses, with
their devices' credentials, from inside its own network.

## The central worker

`compose.yaml`'s `worker` service sets `NEOPS_WORKER_NETWORK_ZONES: default`, as a literal value
in its own service definition, and compose never reads the key from `.env`. For the default zone
there is nothing to change. Keep the key out of `.env` all the same: it is the file a remote site's
worker is most likely to be started from, and from there it would hand that worker the default
zone too.

To give the stock worker other zones, set the key in `compose.override.yaml` and append that file
to `COMPOSE_FILE` (see [Scenarios](20-scenarios.md)). Every snippet on this page goes into that one
file, under one `services:` key with one entry per service: a second `services:` key, or a second
entry for the same service, is a YAML error.

```yaml
services:
  worker:
    environment:
      NEOPS_WORKER_NETWORK_ZONES: default,zone-b
```

## Two workers per zone

Run at least two workers for every zone, `default` included: a zone's jobs, its rollbacks among
them, wait for that zone's own workers for up to `BLACKBOARD_PENDING_JOB_TIMEOUT` (see
[Engine limits](30-operations.md#engine-limits)) and hold up new runs in every zone meanwhile,
as the engine's [page][engine-zones] explains under *Workers and their zones*. The
developer-preview engine (`neops-workflow-engine-preview`) admits 2 workers per installation, so a
zones setup with more workers needs the licensed engine, the one `compose.yaml` pins.

Compose runs one worker. Add the others in `compose.override.yaml` and append it to
`COMPOSE_FILE` (see [Scenarios](20-scenarios.md)). Extending the stock `worker` keeps each added
worker on the image `compose.yaml` pins, so a `git pull && ./neops up` upgrades them together with
the engine, which on 0.x accepts only workers whose engine client is of its own minor version:

```yaml
services:
  worker-2:
    extends: { file: compose.yaml, service: worker }
    environment:
      WORKER_NAME: neops-worker-2
```

`worker-2` is the second central worker and inherits `default`. Give every worker its own
`WORKER_NAME`. Another zone's workers do not belong on the compose host: they share its routing
table with the default workers, so a zone whose addresses overlap the default network's would send
its devices' credentials to whatever answers there. Run them inside the zone's own network, as
described under [Workers at another site](#workers-at-another-site). A worker that serves several
zones lists them all, for example `default,zone-b`, but only zones whose addresses do not overlap
as seen from that worker's host.

`./neops doctor` passes as soon as any worker is online. It does not check zones: a zone whose
workers are all down shows only as runs that wait.

### Host networking

A worker reaches devices through its own networks, and a device address that falls inside one of
them stays inside Docker. Compose's network takes its subnet from Docker's address pools, and
Docker's default bridge is `172.17.0.0/16`. A zone whose management addresses overlap them needs
workers outside those networks.

On the compose host, host networking does not get a worker out of them: Docker's bridges are
routes on the host itself, so a host-networked worker there reaches the same addresses a bridged
one does. Run that zone's workers on a host of their own, inside the zone's network, with host
networking, as described under [Workers at another site](#workers-at-another-site).

## Workers at another site

A zone's workers usually run at the site, on a host inside its network, with host networking. Use
the worker image `compose.yaml` pins (`./neops compose -- config --images` lists it), and move it
with every upgrade of this checkout:

```bash
docker run -d --name neops-worker-b-1 --network host --restart unless-stopped \
  --log-driver json-file --log-opt max-size=20m --log-opt max-file=5 \
  -e URL_BLACKBOARD=http://10.99.0.1:3030 \
  -e DIR_FUNCTION_BLOCKS=neops/fb \
  -e WORKER_NAME=neops-worker-b-1 \
  -e NEOPS_WORKER_NETWORK_ZONES=zone-b \
  quay.io/zebbra/neops-worker-sdk:<pinned tag>
```

`10.99.0.1` stands for an engine address the site can reach. The engine's worker routes are
unauthenticated by design, and the jobs they hand out carry device passwords. Behind the bundled
Traefik those routes answer `403` to every client outside the compose network, so a worker
pointed at Traefik's engine address exits at startup with this line and needs the private path
described below:

```text
Worker registration failed: HTTP 403 Forbidden. Check URL_BLACKBOARD and any proxy in front of the engine.
```

With an external proxy (`compose.expose.yaml`) compose denies nothing itself: your proxy must
deny them (see [External proxy](40-external-proxy.md)), and the engine port that overlay publishes
answers every process that reaches its bind address. A remote site's workers therefore need a
private network path to the engine, such as a site-to-site VPN, and the engine's port published
on an address only that path reaches. Both are yours to build and secure; Neops does not provide
or check them.

Publish the port in `compose.override.yaml` and append that file to `COMPOSE_FILE` (see
[Scenarios](20-scenarios.md)), then run `./neops up`:

```yaml
services:
  engine:
    ports:
      - "10.99.0.1:3030:3030"
```

The first part is the compose host's own address on the private path, the one the site's
`URL_BLACKBOARD` names; the last `3030` is the engine's port inside its container. Always write
the address: without one, or with `0.0.0.0`, Docker publishes the port on every address of the
host. The address must exist on the host when the engine starts, or Docker refuses to start it
with `cannot assign requested address`, so bring the VPN up before `./neops up` and before the
Docker service starts at boot. With `compose.expose.yaml` the port that overlay publishes on
`NEOPS_BIND_ADDRESS` stays as it is, and this one is added beside it.

A zone routes jobs and authorizes nothing: every host the path reaches can register for any zone,
`default` included, and receive its devices' passwords. Restrict the path to the zone's worker
hosts, and encrypt it, with a VPN or with TLS in front of the published port.

### The site host

Each host that runs a zone's workers needs:

- Docker Engine, and a `docker pull` of the pinned worker image that succeeds there. If Quay
  refuses it, run `docker login quay.io` on that host with the account the compose host uses (see
  the prerequisites in `README.md`).
- Outbound connections to the engine's published address and port, and to the zone's devices.
  The worker connects to nothing else of Neops, neither the CMS nor the log store, and nothing
  connects to it: it polls the engine, and its health check reads a local file.
- Log rotation for the worker container. The `docker run` above sets the same rotation compose
  gives its own services; without those options the log grows for as long as the container runs.
- Its own upkeep, since the container is not part of the compose project: `./neops up`, `down`
  and `purge` never start, stop, upgrade or remove it, and `./neops status` does not list it.
  After an upgrade of this checkout, remove it and run it again with the new pinned tag. After a
  `purge` it keeps running and retries an engine that is gone, until you remove it. `./neops doctor`
  counts it only as one of the workers the engine reports online.
- A second worker for the zone, each with its own `WORKER_NAME` and container name, as
  [Two workers per zone](#two-workers-per-zone) explains.

## Site links and outages

The engine delivers a job at least once. A worker cut off from it keeps running the job it holds,
device changes included, while the engine fails that job once it has waited long enough and, for
a block that allows a retry, runs it again on a worker of the zone. When the link comes back,
the engine refuses the cut-off worker's result and its own failure stands (see
[A push after the engine gave up][engine-late-push]). An outage changes nothing only when it is
shorter than `WORKER_OFFLINE_THRESHOLD_MS` and than the worker's `RESULT_PUSH_TIMEOUT`, and the
job still finishes and pushes its result within `BLACKBOARD_STUCK_JOB_TIMEOUT` of the worker
taking it. All three are described below.

The engine fails a job a worker holds at whichever of these comes first, checked every minute
(`BLACKBOARD_JOB_CHECK_INTERVAL`):

| Variable | Default | Counted from | Reason code |
|---|---|---|---|
| `WORKER_OFFLINE_THRESHOLD_MS` | `360000` (6 min) | the worker's last heartbeat | `worker_lost` |
| `BLACKBOARD_STUCK_JOB_TIMEOUT` | `720000` (12 min) | when the worker took the job | `job_timed_out` |

With the defaults, an outage longer than about 6 minutes fails every job the site's workers hold,
whatever their length, and from about 5 minutes a job that finishes during the outage already
loses its result to the worker's `RESULT_PUSH_TIMEOUT` (see below). The stuck timeout fails a long
job even with the link up, so size the offline threshold above the longest outage a site should
ride out, and the stuck timeout above that outage plus the longest job. The engine's
[threshold cascade][engine-thresholds] describes both.

`compose.yaml` passes neither variable to the engine, so `.env` does not reach them. Set them in
`compose.override.yaml`, append that file to `COMPOSE_FILE` (see [Scenarios](20-scenarios.md)),
and run `./neops up`:

```yaml
services:
  engine:
    environment:
      WORKER_OFFLINE_THRESHOLD_MS: "900000"
      BLACKBOARD_STUCK_JOB_TIMEOUT: "1800000"
```

These keys join the engine's entry in the same file (see
[The central worker](#the-central-worker)). With the published port from
[Workers at another site](#workers-at-another-site), that entry reads:

```yaml
services:
  engine:
    ports:
      - "10.99.0.1:3030:3030"
    environment:
      WORKER_OFFLINE_THRESHOLD_MS: "900000"
      BLACKBOARD_STUCK_JOB_TIMEOUT: "1800000"
```

A variable left unset follows the one before it: the stuck timeout defaults to twice the offline
threshold, and `BLACKBOARD_PENDING_JOB_TIMEOUT`, when `.env` leaves it empty, to five times the
stuck timeout, which lengthens how long a zone without workers holds up runs (see
[Two workers per zone](#two-workers-per-zone)). A longer offline threshold also keeps a dead
worker on the license's worker count longer, and after a restart the engine waits that long
before it judges any worker offline.

A site's worker re-sends a result it could not deliver for `RESULT_PUSH_TIMEOUT` seconds (`300`)
from when the job finished, then drops it, and the engine later fails the job although it
succeeded. Set it with `-e RESULT_PUSH_TIMEOUT=...` on the site's workers to at least the longest
outage to ride out, and below the earlier of the two engine settings. That also meets the worker
SDK's [configuration][sdk-config], which asks for less than the stuck timeout.

A job the engine failed this way is retried only when its block is registered pure or idempotent,
and such a block then runs twice, once on the cut-off worker and once on the retry. Any other
block's step fails and goes to the rollback while the cut-off worker's change still lands on the
device. Make the blocks that change devices at a site idempotent, or reversible with a rollback
(see [Retry & Rollback][engine-retry]).

## Creating a zone

1. Make sure every central worker lists `default`. The stock `worker` does, and so does every
   worker that extends it. A worker you defined otherwise is refused at its next start once the
   zone exists.
2. Create the zone on the web client's **Network zones** page, which needs web client 6.1 or
   later and write on the `network-zones` permission element. `cms-init` gives the
   `NEOPS_ADMIN_ROLE` role that write on every `./neops up`, also when other users hold that role
   (see [The first user's permissions](10-install.md#the-first-users-permissions)); grant it to
   any other role that manages zones under *Role permission elements* in the CMS admin site. The
   `networkZoneUpsert` mutation needs the same write, and the superuser can also create the zone
   in the admin site at `<NEOPS_CMS_URL>/admin/`.
   A zone's name is fixed once created; only its description changes. No worker needs to be up.
3. If this is the CMS's first zone, set `NEOPS_WORKER_NETWORK_ZONES` on every worker that lists
   no zone, such as one at a site that registered before, and restart it. The engine checks a
   worker's zones only when it registers, so until then such a worker keeps claiming `default`
   jobs. The Monitor App's **Workers** page flags the workers that registered no zone list.
4. Start the zone's workers. The engine refuses them while the zone does not exist; started too
   early, they exit and, with a restart policy, retry until it does.

A device moves into the zone only through a run, with `device_update_device`'s `network_zone`
parameter. The admin site and `deviceUpsert` cannot move a device, and devices they create are
in `default`.

## Retiring a site

The CMS cannot see which zones the engine still routes jobs to, so retire a site in this order:

1. Move its devices to another zone with `device_update_device`, or delete them, while the site's
   workers still run. A step with `override: true` to a live zone works too, since
   `device_update_device` connects to no device.
2. Stop the site's workers.
3. Delete the zone on the Network zones page, in the admin site or with `networkZoneDelete`, once
   no workflow run names it.

A move or delete left until the workers are gone waits the full hour for a worker that never
comes, and holds every other run meanwhile. For a site that is already gone, delete its devices in
the admin site, which needs no worker. Deleting a device archives it. The zone's delete is refused
while devices that are not archived use it, and clears the zone of its archived devices, so one
restored later is in `default`.

Afterwards, a worker that still lists the zone is refused at its next start, and a run that names
it fails before its first step.

## Running zones

### One unserved zone holds every zone

A run takes its locks in a batch with the other runs that start around it. Once a run of the batch
is about to write back to the CMS, the engine locks no new run until every run of the batch has
ended. So once a run of its batch is about to write back, a run whose job waits for a zone with
no worker keeps new runs in every zone, `default` included, in `locking` until that job runs or
`BLACKBOARD_PENDING_JOB_TIMEOUT` fails it: up to an hour with the default. A site whose network
link or VPN is down is the usual cause, and two workers per zone do not help then. In a test with
a 2 min timeout, new runs in `default` and another zone waited 73 s and 78 s, until the waiting
job failed. The same rule means that one long run, in any zone, holds back new runs once another
run of its batch is about to write back; that follows from the rule and was not measured. A
batch in which no run writes back does not hold new runs, unless a `compose.override.yaml` sets
the engine's `EPOCH_MAX_PARALLEL_RUNS`: reaching that cap holds new runs the same way. Compose
does not set it.

The hold ends when the waiting job runs, that is, when a worker serving its zone polls again:
restore the site's link, or start a worker with that zone in `NEOPS_WORKER_NETWORK_ZONES` on a
host that reaches those devices. Otherwise it ends when `BLACKBOARD_PENDING_JOB_TIMEOUT` fails the
job. Aborting the run does not help: the engine does not implement it and answers `501`.
Restarting the engine does not help either: its runs and jobs are kept in its database, and for
its first 6 min after a restart it fails no never-polled job, so a restart delays the timeout
rather than ending the hold (see [Choosing the pending timeout](#choosing-the-pending-timeout)).

### Seeing it

`./neops doctor` does not check zones. Look in these places instead:

- The Monitor App's **Workers** page shows *Jobs wait in zones no online worker serves*, naming
  each zone and how many of its jobs wait, and flags a zone with only one online worker.
- `GET <NEOPS_ENGINE_URL>/network-zones` lists every zone with `workers.online` and `jobs.pending`
  (needs `worker:read`), and `GET <NEOPS_ENGINE_URL>/blackboard/jobs?status=pending` lists the
  waiting jobs with their `networkZone` and `createdAt` (needs `workflow-execution:read`). Both
  take a CMS access token as `Authorization: Bearer`.

The `operator` workflow profile grants both permissions, so give the account the script uses a
role with it (`grant_workflow_permissions --role <role> --profile operator`, see
[Adding users](10-install.md#adding-users)). The script gets its token from the CMS `login`
mutation, the one `./neops doctor` uses:

```bash
curl -s <NEOPS_CMS_URL>/graphql -H 'Content-Type: application/json' -d \
  '{"query":"mutation{login(username:\"<user>\",password:\"<password>\"){accessToken refreshToken}}"}'
```

The access token expires after 15 min, core's default, so a
long-running script logs in again before each check, or trades its refresh token (valid 7 days,
and replaced by each use) for a new pair with the `refreshToken(refreshToken:)` mutation. The CMS
allows 5 logins a minute from one address. A static API key does not work here: the engine accepts
only the CMS's signed access tokens. With `compose.oidc.yaml`, password login is disabled:
`./neops doctor` then has the CMS issue its token from inside the `cms` container, but compose
ships no command that hands a script a token, so use the Workers page there.

Alert on how long a zone's oldest pending job has waited, against the step time you expect from
[Sizing a zone](#sizing-a-zone), not on `workers.online`. The engine counts a killed worker as
online until `WORKER_UNREACHABLE_THRESHOLD_MS` (2 min by default) has passed since its last ping,
so `workers.online` and the Workers page notice stay quiet that long; a test still saw `online: 1`
86 s after the kill.

### Choosing the pending timeout

`BLACKBOARD_PENDING_JOB_TIMEOUT` (see [Engine limits](30-operations.md#engine-limits)) is both how
long a zone's queue may take and how long an unserved zone may hold every other zone:

- Keep it above the longest wait a job sees in your busiest zone, with margin; see
  [Sizing a zone](#sizing-a-zone). When the timeout fails a job whose zone has a live but busy
  worker, an `EXECUTE` job is retried, but an `ACQUIRE` job is not, and its run fails. In a test
  with a 2 min timeout, 10 of 50 runs started at once failed this way; with the default 1 h the same
  burst completed.
- Keep it no longer than you accept new runs to wait while a site is cut off.

After the engine starts, it fails no never-polled job until it has served for
`WORKER_OFFLINE_THRESHOLD_MS` (6 min by default), so the first such failure after a restart comes
after at least that long, whatever the timeout.

### Sizing a zone

A worker takes one job per poll and then waits about 11 s before it polls again (1 s plus the
worker's default 10 s poll interval), also while jobs wait. With N jobs waiting in a zone at once,
from all runs together, W workers serving it and t seconds per job on the device:

- the last job waits about N / W × (11 s + t), and a run takes about that long per step;
- keep N / W ≤ 3600 / (11 + t) against the default 1 h timeout: about 320 jobs per worker for
  instant jobs, 87 for 30 s jobs, 50 for 60 s jobs;
- for a target step time of T minutes, run W ≥ N × (11 + t) / (60 × T) workers. For example, 120
  devices with t = 20 s and 7.5 min per step need 9 workers.

Measured on engine 0.46.0 and worker 0.5.0 with two workers, 120 devices and a two-step workflow
whose jobs took 0.05 s: 5.4 jobs per worker per minute, a run of 22.5 min, a median wait of 5.5
min and a longest of 11.1 min per job. Runs on the same devices take turns, each starting about 3.5
min after the one before released its locks: three such runs took 22.5, 48.1 and 73.5 min. The
developer-preview engine admits 2 workers in all, across every zone.

### Upgrading with site workers

The 0.46 engine refuses workers built for another engine minor version. So when an upgrade changes
the engine's minor version (the `engine` pin in `compose.yaml`, 0.46 to 0.47 for example), a
site's zone has no worker from `./neops up` until its workers run the new image, and a run that
needs that zone meanwhile holds every zone. Upgrade in this order:

1. Let every run finish, and start none until step 4.
2. Stop the workers at every site.
3. Upgrade this checkout as [Upgrades](30-operations.md#upgrades) describes.
4. Start each site's workers on the worker image `compose.yaml` now pins
   (`./neops compose -- config --images` lists it), and start runs again once every zone shows an
   online worker on the Workers page or in `GET /network-zones`.

### Secrets, backup and restore

- No `./neops rotate` target touches a site's workers: the engine's worker routes are
  unauthenticated, so a worker holds no secret to rotate.
- A worker keeps no state. Nothing at a site needs a backup; the zones and the devices' zones are in
  the CMS database, which `./neops backup` dumps.
- After a restore, `GET /workers` and the Workers page also list the restored engine database's
  worker records, `unreachable` or `offline`, beside the workers that registered again. They are
  left over from the backup and take no jobs.

## Going back

Going back below neops-core 2.4.0 deletes every zone and every device's zone; see
[Going back below 2.4.0](https://github.com/zebbra/neops-core/blob/v2.5.0/docs/releases/v2.4.0.md#going-back-below-240)
in its notes.

[engine-zones]: https://docs.neops.io/neops-workflow-engine/docs/20-workflows/45-network-zones/
[engine-late-push]: https://docs.neops.io/neops-workflow-engine/docs/20-workflows/60-retry-and-rollback/#a-push-after-the-engine-gave-up
[engine-retry]: https://docs.neops.io/neops-workflow-engine/docs/20-workflows/60-retry-and-rollback/
[engine-thresholds]: https://docs.neops.io/neops-workflow-engine/docs/50-deployment/30-worker-management/#threshold-cascade
[sdk-config]: https://docs.neops.io/neops-worker-sdk-py/docs/deployment/20-configuration/
