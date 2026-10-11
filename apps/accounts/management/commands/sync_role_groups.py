"""Synchronize Django role groups with active-year RoleAssignments.

`_require_role` and the dashboards' role counts read Django's native Group
membership, which is normally kept in sync by the RoleAssignment post_save
signal. Accounts created (or migrated) before that signal existed can have a
role assignment but no matching group, so they authenticate but hit a 403 on
their own dashboard. This command re-derives group membership from the
active academic year's assignments for every affected user. Idempotent.
"""

from django.core.management.base import BaseCommand

from apps.accounts.models import RoleAssignment, User, _sync_user_groups


class Command(BaseCommand):
    help = "Sync Django role groups from active-year RoleAssignments for all users."

    def handle(self, *args, **options):
        user_ids = (
            RoleAssignment.objects.filter(academic_year__status="ACTIVE")
            .values_list("user_id", flat=True)
            .distinct()
        )
        users = User.objects.filter(pk__in=list(user_ids))
        count = 0
        for user in users:
            _sync_user_groups(user)
            count += 1
        self.stdout.write(
            self.style.SUCCESS("Synced role groups for {} user(s).".format(count))
        )
