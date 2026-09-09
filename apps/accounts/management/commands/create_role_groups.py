from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group
from apps.accounts.models import Role


class Command(BaseCommand):
    help = "Create Django Groups matching the 6 TASS LMS roles"

    def handle(self, *args, **kwargs):
        for code, label in Role.Code.choices:
            group, created = Group.objects.get_or_create(name=code)
            status = "Created" if created else "Already exists"
            self.stdout.write(f"{status}: {code} ({label})")
        self.stdout.write(self.style.SUCCESS("Role groups synced."))
