"""Give the first superuser a Neops role, so that a fresh install is usable.

Fed to `manage.py shell` by cms-init, which mounts it at /etc/neops/bootstrap_admin_role.py.

Core's GraphQL is gated by the neops_permissions_simple plugin and a Django superuser
carries no Neops role, so without this the account cms-init creates can log in and then
read nothing and write nothing ("User is not allowed to create a group."). The role, the
scope and the grant between them cannot be made over GraphQL either: roleUpsert and
roleScopeUpsert are themselves role-gated, so the very first one has to be written here.

Re-runnable: every step is a get_or_create followed by an explicit re-set.
"""

import os

from django.contrib.auth import get_user_model
from neops.core.models import Scope
from neops.enterprise.permissions.permissions_simple.models import RolePermission, RoleScope

# Permission flags are read=1, execute=2, write=4. Under
# NEOPS_PERMISSIONS_SIMPLE_IMPLICIT_PERMISSION_LEVEL every save() promotes the value to
# 2**bit_length()-1, so storing `write` stores read+execute+write.
FULL = 4
SCOPE_NAME = "Global"
VISIBILITY = ("show_devices", "show_groups", "show_interfaces", "show_clients", "show_topology")

username = os.environ["NEOPS_ADMIN_USER"]
role_name = os.environ.get("NEOPS_ADMIN_ROLE") or "admin"

user = get_user_model().objects.get(username=username)

role, role_created = RolePermission.objects.get_or_create(
    name=role_name, defaults={"default_permission": FULL}
)
role.default_permission = FULL
role.save()
print(f"role {role_name!r} {'created' if role_created else 'present'}")

held = user.roles.filter(pk=role.pk).exists()
user.roles.add(role)
print(f"user {username!r} {'already held' if held else 'granted'} role {role_name!r}")

scope, scope_created = Scope.objects.get_or_create(
    name=SCOPE_NAME, defaults={"description": "Full visibility across all entities"}
)
for field in VISIBILITY:
    setattr(scope, field, True)
scope.save()
print(f"scope {SCOPE_NAME!r} {'created' if scope_created else 'present'}, full visibility")

grant, grant_created = RoleScope.objects.get_or_create(role=role, scope=scope, defaults={"permission": FULL})
grant.permission = FULL
grant.save()
print(f"role {role_name!r} on scope {SCOPE_NAME!r} {'created' if grant_created else 'present'}")
