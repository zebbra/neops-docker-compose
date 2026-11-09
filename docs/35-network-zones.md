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

## Site links and outages

The engine delivers a job at least once. A worker cut off from it keeps running the job it holds,
device changes included, while the engine fails that job once it has waited long enough and, for
a block that allows a retry, runs it again on a worker of the zone. When the link comes back,
the engine refuses the cut-off worker's result and its own failure stands (see
[A push after the engine gave up][engine-late-push]). An outage shorter than both settings below
changes nothing: the worker pushes its result once the link is back.

The engine fails a job a worker holds at whichever of these comes first, checked every minute
(`BLACKBOARD_JOB_CHECK_INTERVAL`):

| Variable | Default | Counted from | Reason code |
|---|---|---|---|
| `WORKER_OFFLINE_THRESHOLD_MS` | `360000` (6 min) | the worker's last heartbeat | `worker_lost` |
| `BLACKBOARD_STUCK_JOB_TIMEOUT` | `720000` (12 min) | when the worker took the job | `job_timed_out` |

With the defaults, an outage longer than about 6 minutes fails every job the site's workers hold,
whatever their length. The stuck timeout fails a long job even with the link up, so size the
offline threshold above the longest outage a site should ride out, and the stuck timeout above
that outage plus the longest job. The engine's [threshold cascade][engine-thresholds] describes
both.

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

## Going back

Going back below neops-core 2.4.0 deletes every zone and every device's zone; see
[Going back below 2.4.0](https://github.com/zebbra/neops-core/blob/v2.4.0/docs/releases/v2.4.0.md#going-back-below-240)
in its notes.

[engine-zones]: https://docs.neops.io/neops-workflow-engine/docs/20-workflows/45-network-zones/
[engine-late-push]: https://docs.neops.io/neops-workflow-engine/docs/20-workflows/60-retry-and-rollback/#a-push-after-the-engine-gave-up
[engine-retry]: https://docs.neops.io/neops-workflow-engine/docs/20-workflows/60-retry-and-rollback/
[engine-thresholds]: https://docs.neops.io/neops-workflow-engine/docs/50-deployment/30-worker-management/#threshold-cascade
[sdk-config]: https://docs.neops.io/neops-worker-sdk-py/docs/deployment/20-configuration/
