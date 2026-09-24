#!/usr/bin/env python3
"""Browser sign-in through the bundled Keycloak, against a stack run_scenario.py left running.

    uv run --with playwright playwright install chromium          # once per machine
    uv run --with playwright python tests/e2e/keycloak_login.py /path/to/clone

Creates a realm user and a `neops-auth` client role through Keycloak's admin REST API, drives
Chromium through the web client's "Login with Keycloak" button, and asserts that the app leaves
/login with a Neops token in localStorage and that the role claim reached core. Exit code 0 only
when every assertion held; a failure leaves screenshots and the page HTML beside the clone.

`--create-realm-role` and `--assert-realm-role` skip the browser and only touch the admin API.
They are how a chaos run proves that a `down` and `up` does not re-import the realm over
whatever was configured in the admin console.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(REPO), str(HERE)]

from run_scenario import CONNECT, Report  # noqa: E402

from neops_compose.doctor import Http  # noqa: E402
from neops_compose.env import Env  # noqa: E402
from neops_compose.render import KEYCLOAK_CLIENT_ID, KEYCLOAK_REALM, keycloak_relative_path  # noqa: E402
from neops_compose.urls import PublicUrl  # noqa: E402

# A throwaway local stack: these are fixtures, not credentials worth protecting. Fixed rather
# than generated so a second run (after a restart, or after a secret rotation) reuses the user.
TEST_USER = "e2e-keycloak"
TEST_EMAIL = "e2e-keycloak@neops.localhost"
TEST_PASSWORD = "e2e-keycloak-Passw0rd"
TEST_ROLE = "neops-e2e"
BUTTON = "button:has-text('Login with Keycloak')"
TOKEN_KEY = "token"
# Mid-navigation the main frame can be an opaque document, where reading localStorage throws
# SecurityError; an exception would abort the wait, so the predicate swallows it and polls on.
HAS_TOKEN = (
    f"() => {{ try {{ return !!window.localStorage.getItem('{TOKEN_KEY}') }} catch {{ return false }} }}"
)
TIMEOUT_MS = 60_000  # the box can be busy running several stacks; the SPA boot is the slow part


class KeycloakError(RuntimeError):
    pass


class KeycloakAdmin:
    """Keycloak's admin REST API on the loopback port the overlay always publishes.

    The public URL goes through Traefik and a self-signed certificate; the loopback port is
    plain HTTP and needs no trust store, which is why the harness uses it for setup.
    """

    def __init__(self, base: str, password: str, user: str = "admin"):
        self.base = base.rstrip("/")
        self.token = self._password_grant(user, password)

    def _password_grant(self, user: str, password: str) -> str:
        body = urllib.parse.urlencode(
            {"grant_type": "password", "client_id": "admin-cli", "username": user, "password": password}
        ).encode()
        req = urllib.request.Request(
            f"{self.base}/realms/master/protocol/openid-connect/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)["access_token"]

    def request(self, method: str, path: str, payload: object = None) -> tuple[int, object]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode()
                return resp.status, (json.loads(text) if text.strip() else None)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode(errors="replace")

    def _expect(self, method: str, path: str, payload: object, allowed: tuple[int, ...]) -> object:
        status, body = self.request(method, path, payload)
        if status not in allowed:
            raise KeycloakError(f"{method} {path} -> {status} {str(body)[:200]}")
        return body

    def ensure_user(self, username: str, email: str, password: str) -> str:
        """Idempotent: 409 means a previous run created the user, whose password is then reset."""
        self._expect(
            "POST",
            f"/admin/realms/{KEYCLOAK_REALM}/users",
            {
                "username": username,
                "email": email,
                "emailVerified": True,
                "enabled": True,
                "firstName": "E2E",
                "lastName": "Keycloak",
                "credentials": [{"type": "password", "value": password, "temporary": False}],
            },
            (201, 409),
        )
        query = urllib.parse.urlencode({"username": username, "exact": "true"})
        found = self._expect("GET", f"/admin/realms/{KEYCLOAK_REALM}/users?{query}", None, (200,))
        if not found:
            raise KeycloakError(f"user {username} is missing right after being created")
        user_id = found[0]["id"]
        self._expect(
            "PUT",
            f"/admin/realms/{KEYCLOAK_REALM}/users/{user_id}/reset-password",
            {"type": "password", "value": password, "temporary": False},
            (204,),
        )
        return user_id

    def client_uuid(self, client_id: str) -> str:
        query = urllib.parse.urlencode({"clientId": client_id})
        found = self._expect("GET", f"/admin/realms/{KEYCLOAK_REALM}/clients?{query}", None, (200,))
        if not found:
            raise KeycloakError(f"the realm has no client {client_id}")
        return found[0]["id"]

    def grant_client_role(self, user_id: str, client_uuid: str, role: str) -> None:
        """The role claim is authorization-critical: core mirrors resource_access into user.roles."""
        base = f"/admin/realms/{KEYCLOAK_REALM}/clients/{client_uuid}/roles"
        self._expect("POST", base, {"name": role}, (201, 409))
        representation = self._expect("GET", f"{base}/{role}", None, (200,))
        self._expect(
            "POST",
            f"/admin/realms/{KEYCLOAK_REALM}/users/{user_id}/role-mappings/clients/{client_uuid}",
            [representation],
            (204,),
        )

    def create_realm_role(self, role: str) -> None:
        self._expect("POST", f"/admin/realms/{KEYCLOAK_REALM}/roles", {"name": role}, (201, 409))

    def has_realm_role(self, role: str) -> bool:
        status, _ = self.request("GET", f"/admin/realms/{KEYCLOAK_REALM}/roles/{role}")
        return status == 200


@dataclass(frozen=True)
class LoginResult:
    url: str
    token: str
    error: str = ""


def _record(page, shots: Path, name: str) -> None:
    shots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(shots / f"{name}.png"), full_page=True)
    (shots / f"{name}.html").write_text(page.content())


def browser_login(web_url: str, username: str, password: str, shots: Path) -> LoginResult:
    """Chromium resolves *.localhost to 127.0.0.1 itself, so no host mapping is needed."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        try:
            page.goto(web_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            page.wait_for_selector(BUTTON, timeout=TIMEOUT_MS).click()
            page.wait_for_selector("#username", timeout=TIMEOUT_MS).fill(username)
            page.fill("#password", password)
            page.click("#kc-login")
            page.wait_for_url(lambda url: url.startswith(web_url), timeout=TIMEOUT_MS)
            page.wait_for_function(HAS_TOKEN, timeout=TIMEOUT_MS)
            token = page.evaluate(f"() => window.localStorage.getItem('{TOKEN_KEY}')")
            page.wait_for_url(lambda url: "/auth/callback" not in url, timeout=TIMEOUT_MS)
            _record(page, shots, "logged-in")
            return LoginResult(page.url, token)
        except PlaywrightError as exc:
            _record(page, shots, "failure")
            return LoginResult(page.url, "", str(exc).splitlines()[0])
        finally:
            context.close()
            browser.close()


def core_user(cms: PublicUrl, token: str, username: str) -> tuple[int, str]:
    http = Http(connect=CONNECT, insecure=True)
    query = json.dumps(
        {"query": f'{{users(username:"{username}"){{results{{username email roles{{name}}}}}}}}'}
    )
    return http.request(cms, "/graphql", method="POST", body=query, headers={"Authorization": token})


def keycloak_admin(env: Env) -> KeycloakAdmin:
    port = env.get("NEOPS_KEYCLOAK_PORT", "8180")
    rel = keycloak_relative_path(env).rstrip("/")
    return KeycloakAdmin(f"http://{CONNECT}:{port}{rel}", env.require("NEOPS_KEYCLOAK_ADMIN_PASSWORD"))


def login_flow(clone: Path, env: Env, report: Report) -> None:
    admin = keycloak_admin(env)
    user_id = admin.ensure_user(TEST_USER, TEST_EMAIL, TEST_PASSWORD)
    admin.grant_client_role(user_id, admin.client_uuid(KEYCLOAK_CLIENT_ID), TEST_ROLE)
    report.add(True, f"realm user {TEST_USER} has the {TEST_ROLE} role on {KEYCLOAK_CLIENT_ID}")

    result = browser_login(
        env.require("NEOPS_WEB_URL"), TEST_USER, TEST_PASSWORD, clone.parent / "playwright"
    )
    if not report.add(
        bool(result.token), f"login stores a token in localStorage ({result.error} at {result.url})"
    ):
        return
    report.add("/login" not in result.url, f"the app navigated away from /login (now {result.url})")
    report.add(result.token.startswith("Bearer "), "the stored token is a bearer token")

    status, text = core_user(PublicUrl.parse(env.require("NEOPS_CMS_URL")), result.token, TEST_USER)
    report.add(status == 200 and TEST_EMAIL in text, f"core knows the SSO user ({status}: {text[:160]})")
    report.add(TEST_ROLE in text, f"the Keycloak client role reached core ({text[:200]})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clone", type=Path, help="the clone run_scenario.py printed as 'workdir <dir>/repo'")
    ap.add_argument("--create-realm-role", metavar="NAME", help="create a realm role and exit")
    ap.add_argument("--assert-realm-role", metavar="NAME", help="assert a realm role still exists")
    args = ap.parse_args(argv)
    if not (args.clone / ".env").is_file():
        raise SystemExit(f"{args.clone}/.env is missing; pass the clone, not the workdir")

    env = Env(args.clone / ".env")
    report = Report()
    if args.create_realm_role:
        keycloak_admin(env).create_realm_role(args.create_realm_role)
        report.add(True, f"realm role {args.create_realm_role} created")
    elif args.assert_realm_role:
        name = args.assert_realm_role
        report.add(keycloak_admin(env).has_realm_role(name), f"realm role {name} survived the restart")
    else:
        login_flow(args.clone, env, report)

    print(f"\n{len(report.results) - len(report.failed)}/{len(report.results)} assertions passed")
    for message in report.failed:
        print(f"  FAILED: {message}", flush=True)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
