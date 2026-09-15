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

Route each `NEOPS_*_URL` hostname to the matching port. Whatever proxy you use, it must satisfy
three rules or the deployment misbehaves in ways that are easy to misdiagnose.

## 1. Overwrite `X-Forwarded-Proto` and `X-Real-IP`

Core hardcodes `SECURE_PROXY_SSL_HEADER` to read `X-Forwarded-Proto`, and (once you enable
`RATELIMIT_IP_META_KEY=HTTP_X_REAL_IP`, see below) its login rate limiter reads `X-Real-IP`. Both
headers must be **overwritten**, not appended to — a proxy that appends lets a client set its own
`X-Real-IP` and forge its way around the per-address login limit, and a client that sets
`X-Forwarded-Proto` itself can make core believe an insecure request was HTTPS. Terminate TLS at
the proxy and set both headers explicitly rather than trusting whatever the client sent.

## 2. Deny the engine's worker routes

The workflow engine's blackboard API is unauthenticated by design — it is meant to be reached
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

Everything else on the engine's hostname must reach it normally — the monitor app needs
`GET /workers`, `GET /function-blocks` and `GET /blackboard/jobs`, all of which are
permission-guarded, so the deny list has to be exact rather than a wholesale block of the engine.

## 3. Allow large request bodies to the CMS

Set at least a 200 MB body-size limit on the CMS's hostname — reports and file uploads exceed the
usual default.

## nginx

```nginx
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
    reverse_proxy 127.0.0.1:8080
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
./neops doctor --probe-ratelimit
```

`doctor` always sends a `POST /blackboard/job` to the public engine URL and warns in its report if
the response is not `403` — in external-proxy mode this cannot fail the overall check (compose
does not control your proxy), so read the report, not just its exit code. `--probe-ratelimit` adds
six login attempts with six different forged `X-Real-IP` values: if none of them is rate-limited,
your proxy is appending to the header instead of overwriting it. Only set
`RATELIMIT_IP_META_KEY=HTTP_X_REAL_IP` in `.env` once this probe passes — with it set but the
header not actually overwritten by your proxy, *every* login fails with a 500.
