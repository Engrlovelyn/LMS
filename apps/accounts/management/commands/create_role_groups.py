from django.core.management.base import BaseCommand

from apps.accounts.rbac import ROLE_PERMISSIONS
from apps.accounts.models import Role


class Command(BaseCommand):
    help = "Create Django role groups and sync their permissions from the RBAC matrix"

    def handle(self, *args, **kwargs):
        from apps.accounts.rbac import sync_role_permissions

        for code, label in Role.Code.choices:
            self.stdout.write(
                f"{code} ({label}): {len(ROLE_PERMISSIONS.get(code, set()))} permissions mapped"
            )

        results = sync_role_permissions(apply=True)
        for code, count, missing in results:
            note = ""
            if missing:
                note = f"  [MISSING: {', '.join(missing)}]"
            self.stdout.write(f"Synced: {code} -> {count} permissions granted{note}")

        self.stdout.write(self.style.SUCCESS("Role groups synced against the RBAC matrix."))