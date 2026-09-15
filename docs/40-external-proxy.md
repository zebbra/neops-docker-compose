---
title: External proxy
description: The header and denial contract a reverse proxy must satisfy in front of compose.expose.yaml, with nginx and Caddy examples.
tags: [howto, reference]
---

# External proxy

`compose.expose.yaml` (`examples/external-proxy.env`) runs no bundled Traefik. Instead it
publishes each browser-facing service on `127.0.0.1` (or `NEOPS_BIND_ADDRESS`), and you point your
own reverse proxy at them:

| Service | Default port | `.env` variable |
|---|---|---|
| web client | 8080 | `NEOPS_WEB_PORT` |
| CMS | 8000 | `NEOPS_CMS_PORT` |
| workflow engine | 3030 | `NEOPS_ENGINE_PORT` |
| monitor app (workflow manager) | 3031 | `NEOPS_MONITOR_PORT` |
| Keycloak, if used | 8180 | `NEOPS_KEYCLOAK_PORT` |
| Grafana, if used | 3000 | `NEOPS_GRAFANA_PORT` |

Route each `NEOPS_*_URL` hostname to the matching port. If a URL carries a path (the engine or
the monitor under a prefix of the web client's hostname), route that prefix to the port and strip
it before forwarding; the monitor learns its prefix from `NEOPS_WORKFLOWS_URL` on its own.
Whatever proxy you use, it must satisfy four rules or the deployment misbehaves in ways that are
easy to misdiagnose.

## 1. Overwrite `X-Forwarded-Proto` and `X-Real-IP`

Core hardcodes `SECURE_PROXY_SSL_HEADER` to read `X-Forwarded-Proto`, and (once you enable
`RATELIMIT_IP_META_KEY=HTTP_X_REAL_IP`, see below) its login rate limiter reads `X-Real-IP`. Both
headers must be **overwritten**, not appended to. A proxy that appends lets a client set its own
`X-Real-IP` and forge its way around the per-address login limit, and a client that sets
`X-Forwarded-Proto` itself can make core believe an insecure request was HTTPS. Terminate TLS at
the proxy and set both headers explicitly rather than trusting whatever the client sent.

## 1b. Redirect the bare `/admin` and `/accounts`

Core answers `/admin` and `/accounts` without a trailing slash with a 500 instead of Django's
usual slash redirect (neops-core #2276). The bundled Traefik redirects both to their slashed
form; an external proxy should do the same (`/admin` to `/admin/`, `/accounts` to
`/accounts/`), or accept that a hand-typed `https://cms.example.com/admin` errors.

## 2. Deny the engine's worker routes

The workflow engine's blackboard API is unauthenticated by design: it is meant to be reached
only from workers on the private compose network, not from a browser. The routes below must
return `403` (or otherwise never reach the engine) from the public engine URL. They are listed in
`neops_compose/routes.py` alongside a comment naming their source in the engine's controllers, all
`POST`, matched case-insensitively with an optional trailing slash (that is how the engine itself,
built on Express, matches them):

```
/blackboard/job
/blackboard/job/*
/workers/register
/workers/*/ping
/workers/*/unregister
/function-blocks/register
```

Everything else on the engine's hostname must reach it normally. The monitor app needs
`GET /workers`, `GET /function-blocks/registrations/list` and `GET /blackboard/jobs`, all of which
are permission-guarded, so the deny list has to be exact rather than a wholesale block of the
engine.

## 3. Allow large request bodies to the CMS

Set at least a 200 MB body-size limit on the CMS's hostname: reports and file uploads exceed the
usual default.

## 4. Forward WebSocket upgrades to the CMS

Core serves GraphQL subscriptions over WebSocket on `/graphql`, the same path as the query
endpoint, and accepts only the `graphql-ws` subprotocol. Caddy forwards an upgrade without being
asked. nginx does not: it speaks HTTP/1.0 upstream and drops the hop-by-hop `Upgrade` and
`Connection` headers, so the handshake arrives at core as an ordinary GET and comes back `400`
with nothing in any log to say why. The snippet below carries the three lines that fix it.

## nginx

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

map $request_method $engine_deny {
    default 0;
    POST    1;
}

server {
    listen 443 ssl;
    server_name neops.example.com;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 443 ssl;
    server_name cms.neops.example.com;
    client_max_body_size 200m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 443 ssl;
    server_name engine.neops.example.com;

    location ~* ^/(blackboard/job(/.*)?|workers/register/?|workers/[^/]+/(ping|unregister)/?|function-blocks/register/?)$ {
        if ($engine_deny) { return 403; }
        proxy_pass http://127.0.0.1:3030;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location / {
        proxy_pass http://127.0.0.1:3030;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 443 ssl;
    server_name workflows.neops.example.com;
    location / {
        proxy_pass http://127.0.0.1:3031;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Caddy

```caddyfile
neops.example.com {
    reverse_proxy 127.0.0.1:8080 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}

cms.neops.example.com {
    request_body {
        max_size 200MB
    }
    reverse_proxy 127.0.0.1:8000 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}

engine.neops.example.com {
    @worker_api {
        method POST
        path_regexp (?i)^/(blackboard/job(/.*)?|workers/register/?|workers/[^/]+/(ping|unregister)/?|function-blocks/register/?)$
    }
    respond @worker_api 403
    reverse_proxy 127.0.0.1:3030 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}

workflows.neops.example.com {
    reverse_proxy 127.0.0.1:3031 {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

## Verify it

```bash
./neops doctor
```

`tests/e2e/caddy/Caddyfile` is the Caddy snippet above with the e2e harness's hostnames and
ports, and `tests/e2e/external_proxy_check.py` runs it in front of a live external-proxy stack
and asserts all four rules. Keep the two in step: that script is what proves the snippet works.

`doctor` always sends a `POST /blackboard/job` to the public engine URL and fails the
`engine worker API denied` probe if the response is not `403`. This is enforced in every
scenario, including external-proxy mode, so a reachable worker route fails `doctor` outright.

The rate-limit probe needs a different order, because it only tests anything once
`RATELIMIT_IP_META_KEY` is set: while that key is unset, core rate-limits on the raw TCP source
address rather than `X-Real-IP`, so `--probe-ratelimit` passes trivially regardless of what your
proxy does with the header.

```bash
# 1. Set it and apply it.
echo 'RATELIMIT_IP_META_KEY=HTTP_X_REAL_IP' >> .env
./neops up

# 2. Now the probe means something.
./neops doctor --probe-ratelimit
```

`--probe-ratelimit` sends six login attempts with six different forged `X-Real-IP` values: if none
of them is rate-limited, your proxy is not overwriting the header (appending to it, or passing the
client's own value through). If the probe fails, unset `RATELIMIT_IP_META_KEY` again and fix the
proxy first. Leaving the key set with a proxy that does not reliably overwrite the header is what
turns every login into a 500 (see [Troubleshooting](50-troubleshooting.md#500-on-login-ratelimit_ip_meta_key)).
