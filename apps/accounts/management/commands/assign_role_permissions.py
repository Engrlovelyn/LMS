from django.core.management.base import BaseCommand

from apps.accounts.rbac import ROLE_PERMISSIONS, sync_role_permissions


class Command(BaseCommand):
    help = (
        "Apply the official User Role Matrix (manuscript Table 84) to the role "
        "Groups, using apps.accounts.rbac as the single source of truth. "
        "Idempotent; safe to run repeatedly."
    )

    def handle(self, *args, **kwargs):
        results = sync_role_permissions(apply=True)

        total = sum(granted for _code, granted, _missing in results)
        role_codes = sorted(code for code, _granted, _missing in results)

        self.stdout.write(self.style.SUCCESS(
            f"Synced {len(role_codes)} role groups from the User Role Matrix "
            f"({total} group-permission links total)."
        ))
        self.stdout.write("  Roles: " + ", ".join(role_codes))

        unresolved = [
            (code, q)
            for code, _granted, missing in results
            for q in missing
        ]
        if unresolved:
            self.stdout.write(self.style.WARNING(
                f"{len(unresolved)} permission codename(s) could not be resolved "
                f"to a Permission object (check app Meta.permissions):"
            ))
            for code, q in unresolved:
                self.stdout.write(self.style.WARNING(f"  - {q} (role: {code})"))
        else:
            self.stdout.write(self.style.SUCCESS(
                "All matrix permission codenames resolved to real Permission objects."
            ))