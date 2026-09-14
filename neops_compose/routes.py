"""Path knowledge shared by the routing overlays.

CORE_PREFIXES: root-absolute URL prefixes neops-core serves, from
neops-core/backend/neopsapp/urls.py plus the neops_webhook plugin. In
shared-hostname mode these are routed to the CMS on the web client's hostname.
Adding a URL prefix to core is a two-repo change: update this list too.

ENGINE_PUBLIC_WORKER_ROUTES: the engine's @Public() worker routes
(neops-workflow-engine src/resources/{blackboard,workers,function-blocks}/*.controller.ts).
They are unauthenticated by design and must never be reachable from outside the
compose network. All are POST. A new @Public() route in the engine is a two-repo change.

WEB_RESERVED_PATHS: paths the web client SPA owns on its origin.
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

# (matcher, value) pairs; traefik_model turns them into a Traefik v3 rule and
# prepends the engine's public path prefix when it has one. All are POST.
ENGINE_PUBLIC_WORKER_ROUTES: tuple[tuple[str, str], ...] = (
    ("path", "/blackboard/job"),
    ("prefix", "/blackboard/job/"),
    ("path", "/workers/register"),
    ("regexp", "/workers/[^/]+/(ping|unregister)"),
    ("path", "/function-blocks/register"),
)

WEB_RESERVED_PATHS: tuple[str, ...] = ("/auth", "/login")
