"""Path knowledge shared by the routing overlays.

CORE_PREFIXES: root-absolute URL prefixes neops-core serves, from
neops-core/backend/neopsapp/urls.py plus the neops_webhook plugin. In
shared-hostname mode these are routed to the CMS on the web client's hostname.
Adding a URL prefix to core is a two-repo change: update this list too.

ENGINE_PUBLIC_WORKER_ROUTES: the engine's @Public() worker routes
(neops-workflow-engine src/resources/{blackboard,workers,function-blocks}/*.controller.ts).
They are unauthenticated by design and must never be reachable from outside the
compose network. All are POST. A new @Public() route in the engine is a two-repo change.

Express, which the engine runs on, matches these case-insensitively and tolerates
a trailing slash; Traefik's Path/PathPrefix matchers do neither. traefik_model
therefore joins these fragments (each relative, no leading slash) into a single
case-insensitive, slash-tolerant PathRegexp rather than matching each verbatim.

WEB_RESERVED_PATHS: paths the web client SPA owns on its origin: its auth routes and
the /monitor route that embeds the workflow manager (routeMonitor in
neops-web-client src/app/app-routing/routing-keys.ts).
"""

CORE_PREFIXES: tuple[str, ...] = (
    "/graphql",
    "/graphiql",
    "/admin",
    "/djstatic",
    "/.well-known",
    "/accounts",
    "/auth/oidc-login",
    "/auth/oidc-complete",
    "/auth/oidc-logout",
    "/webhook",
)

ENGINE_PUBLIC_WORKER_ROUTES: tuple[str, ...] = (
    "blackboard/job(/.*)?",
    "workers/register",
    "workers/[^/]+/(ping|unregister)",
    "function-blocks/register",
)

WEB_RESERVED_PATHS: tuple[str, ...] = ("/auth", "/login", "/monitor")
