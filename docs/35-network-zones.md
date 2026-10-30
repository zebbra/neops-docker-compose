---
title: Network zones
description: Running workers per network zone on a compose install, creating the first zone, and retiring a site.
tags: [howto, operations]
---

# Network zones

A network zone names a network whose device addresses may overlap another's. Every device is in
exactly one zone, and only a worker that serves a job's zone can claim it. A device nobody
assigned is in `default`, which is also the zone every worker serves while the CMS holds no other.
Zones need neops-core 2.4, workflow engine 0.45 and worker 0.4 or later.

A worker serves the zones listed in `NEOPS_WORKER_NETWORK_ZONES`, comma-separated. Once the CMS
holds any zone, the engine refuses a worker that lists none: it exits at startup and asks for the
setting. That protects a remote site whose worker lost its setting, which would otherwise reach
the default network's addresses, with their devices' credentials, from inside its own network.

## The central worker

`compose.yaml`'s `worker` service sets `NEOPS_WORKER_NETWORK_ZONES: default`, as a literal value
in its own service definition. There is nothing to change. Keep the setting out of `.env`: it is
the file a remote site's worker is most likely to be started from, and from `.env` it would hand
that worker the default zone too. Older workers ignore the setting.

## Two workers per zone

Run at least two workers for every zone, `default` included. A rollback goes back to its forward
job's zone, and the worker it needs is often the one that just died. A zone with one worker also
holds up its runs during every restart of that worker, and a job no worker of its zone claims
waits up to `BLACKBOARD_PENDING_JOB_TIMEOUT` (one hour, see
[Engine limits](30-operations.md#engine-limits)) before it fails. A waiting `EXECUTE` job holds
the engine's drain phase, so new runs everywhere wait with it.

Compose runs one worker. Add the others in `compose.override.yaml` and append it to
`COMPOSE_FILE` (see [Scenarios](20-scenarios.md)). Extending the stock `worker` keeps each added
worker on the image `compose.yaml` pins, so a `git pull && ./neops up` upgrades them together with
the engine, which on 0.x accepts only workers of its own minor version:

```yaml
services:
  worker-2:
    extends: { file: compose.yaml, service: worker }
    environment:
      WORKER_NAME: neops-worker-2

  worker-zone-b-1:
    extends: { file: compose.yaml, service: worker }
    environment:
      WORKER_NAME: neops-worker-zone-b-1
      NEOPS_WORKER_NETWORK_ZONES: zone-b
  worker-zone-b-2:
    extends: { file: compose.yaml, service: worker }
    environment:
      WORKER_NAME: neops-worker-zone-b-2
      NEOPS_WORKER_NETWORK_ZONES: zone-b
```

`worker-2` is the second central worker and inherits `default`. The `zone-b` workers list their
own zone instead; one that also serves the default network must list `default,zone-b`. Give every
worker its own `WORKER_NAME`.

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
Traefik those routes answer `403` to every client outside the compose network. With an external
proxy (`compose.expose.yaml`) compose denies nothing itself: your proxy must deny them (see
[External proxy](40-external-proxy.md)), and the engine port that overlay publishes answers every
process that reaches its bind address. A remote site's workers therefore need a private network
path to the engine, such as a site-to-site VPN, and a `compose.override.yaml` you write that
publishes the engine's port on an address only that path reaches. Both are yours to build and
secure; Neops does not provide or check them.

## Creating a zone

1. Make sure every central worker lists `default`. The stock `worker` does, and so does every
   worker that extends it. A worker you defined otherwise is refused at its next start once the
   zone exists.
2. Create the zone, as the superuser in the CMS admin site at `<NEOPS_CMS_URL>/admin/`, or with
   the `networkZoneUpsert` mutation. The mutation needs write on the `network-zones` permission
   element, which no role holds by default, `NEOPS_ADMIN_ROLE` included: grant it to the roles
   that manage zones under *Role permission elements* in the admin site.
   A zone's name is fixed once created; only its description changes. No worker needs to be up.
3. Start the zone's workers. The engine refuses them while the zone does not exist; started too
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
3. Delete the zone, in the admin site or with `networkZoneDelete`, once no workflow run names it.

A move or delete left until the workers are gone waits the full hour for a worker that never
comes, and holds every other run meanwhile. For a site that is already gone, delete its devices in
the admin site, which needs no worker. Deleting a device archives it. The zone's delete is refused
while devices that are not archived use it, and clears the zone of its archived devices, so one
restored later is in `default`.

Afterwards, a worker that still lists the zone is refused at its next start, and a run that names
it fails before its first step.
