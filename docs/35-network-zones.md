---
title: Network zones
description: Running workers per network zone on a compose install, creating the first zone, and retiring a site.
tags: [howto, operations]
---

# Network zones

A network zone is a network that only its own workers reach, so only a worker that serves a
job's zone can claim it; a device nobody assigned is in `default`. The workflow engine's
[Network Zones][engine-zones] page owns the rules; this page covers what is particular to a
compose install. Zones need neops-core 2.4, workflow engine 0.46 and worker 0.5 or later.

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
to `COMPOSE_FILE` (see [Scenarios](20-scenarios.md)):

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
private network path to the engine, such as a site-to-site VPN, and a `compose.override.yaml` you
write that publishes the engine's port on an address only that path reaches. Both are yours to
build and secure; Neops does not provide or check them.

A zone routes jobs and authorizes nothing: every host the path reaches can register for any zone,
`default` included, and receive its devices' passwords. Restrict the path to the zone's worker
hosts, and encrypt it, with a VPN or with TLS in front of the published port.

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
ended. So once a run of its batch has written back, a run whose job waits for a zone with no
worker keeps new runs in every zone, `default` included, in `locking` until that job runs or
`BLACKBOARD_PENDING_JOB_TIMEOUT` fails it: up to an hour with the default. A site whose network link or VPN is down is the usual cause, and
two workers per zone do not help then. In a test with a 2 min timeout, new runs in `default` and
another zone waited 73 s and 78 s, until the waiting job failed. The same rule means that one long
run, in any zone, holds back new runs once another run of its batch writes back; that follows from
the rule and was not measured. A batch in which no run writes back does not hold new runs, unless
a `compose.override.yaml` sets the engine's `EPOCH_MAX_PARALLEL_RUNS`: reaching that cap holds new
runs the same way. Compose does not set it.

### Seeing it

`./neops doctor` does not check zones. Look in these places instead:

- The Monitor App's **Workers** page shows *Jobs wait in zones no online worker serves*, naming
  each zone and how many of its jobs wait, and flags a zone with only one online worker.
- `GET <NEOPS_ENGINE_URL>/network-zones` lists every zone with `workers.online` and `jobs.pending`
  (needs `worker:read`), and `GET <NEOPS_ENGINE_URL>/blackboard/jobs?status=pending` lists the
  waiting jobs with their `networkZone` and `createdAt` (needs `workflow-execution:read`). Both
  take a CMS access token as `Authorization: Bearer`.

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

The 0.46 engine refuses workers of another minor version, so a site's zone has no worker from
`./neops up` until its workers run the new image, and a run that needs that zone meanwhile holds
every zone. Upgrade in this order:

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
[Going back below 2.4.0](https://github.com/zebbra/neops-core/blob/v2.4.0/docs/releases/v2.4.0.md#going-back-below-240)
in its notes.

[engine-zones]: https://docs.neops.io/neops-workflow-engine/docs/20-workflows/45-network-zones/
