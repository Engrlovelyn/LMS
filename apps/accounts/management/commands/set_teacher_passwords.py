"""Set deterministic demo passwords for staff accounts.

Every account holding the ``teacher`` role gets the password
``<first name>12345`` (e.g. ``lolet12345``), matching the seeded official
accounts. Imported staff previously got a random password that was discarded,
so none of them could sign in.

    python manage.py set_teacher_passwords            # apply
    python manage.py set_teacher_passwords --dry-run  # list, change nothing
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.accounts.demo_passwords import default_password_for
from apps.accounts.models import RoleAssignment, User


class Command(BaseCommand):
    help = "Set every teacher's password to <first name>12345 (pilot/demo use)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the passwords that would be set without saving them.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        users = (
            User.objects.filter(role_assignments__role__code="teacher")
            .select_related("employee_profile")
            .distinct()
            .order_by("id")
        )

        changed = 0
        skipped = []
        for user in users:
            profile = getattr(user, "employee_profile", None)
            first_name = (getattr(profile, "first_name", "") or "").strip()
            if not first_name:
                skipped.append(user.email)
                continue
            password = default_password_for(first_name)
            if dry_run:
                self.stdout.write("{} -> {}".format(user.email, password))
            else:
                with transaction.atomic():
                    user.set_password(password)
                    user.password_changed_at = timezone.now()
                    user.save(update_fields=["password", "password_changed_at"])
            changed += 1

        verb = "would set" if dry_run else "set"
        self.stdout.write(
            self.style.SUCCESS("{} password(s) {} for teacher accounts.".format(changed, verb))
        )
        for email in skipped:
            self.stdout.write(
                self.style.WARNING("skipped (no first name on profile): {}".format(email))
            )
        self.stdout.write("Role assignments checked: {}".format(RoleAssignment.objects.count()))
