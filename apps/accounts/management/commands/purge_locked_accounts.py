"""Remove the sample locked accounts created by ``seed_locked_accounts``.

Only rows that carry the seeder's marker email prefix are deleted, so this
cannot remove a genuine employee or student account even if it is currently
locked. A ``purge_demo_data`` style "delete everything" would be far too
dangerous to run against an account table holding real lockouts.
"""

from django.core.management.base import BaseCommand

from apps.accounts.models import User

from .seed_locked_accounts import SEED_EMAIL_PREFIX


class Command(BaseCommand):
    help = "Delete the seeded sample locked accounts (those on the locked.invalid domain)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        qs = User.objects.filter(email__startswith=SEED_EMAIL_PREFIX)
        count = qs.count()
        if not count:
            self.stdout.write("No seeded locked accounts found. Nothing to do.")
            return

        if options.get("dry_run"):
            self.stdout.write(
                self.style.WARNING(
                    "Would delete {} account(s):".format(count)
                )
            )
            for email in qs.order_by("email").values_list("email", flat=True):
                self.stdout.write("  {}".format(email))
            return

        # Their profiles, role assignments, sessions, tokens and activity log
        # rows all cascade from the User row.
        qs.delete()
        self.stdout.write(
            self.style.SUCCESS("Deleted {} seeded locked account(s).".format(count))
        )
        self.stdout.write(
            "{} locked account(s) remain (genuine lockouts).".format(
                User.objects.filter(status=User.Status.LOCKED).count()
            )
        )