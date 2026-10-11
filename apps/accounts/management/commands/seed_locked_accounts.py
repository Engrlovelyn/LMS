"""Seed locked accounts so the System Administrator's recovery UI has real rows.

The Security Center's Locked Accounts card and the User Accounts table are the
only recovery paths an administrator has when someone cannot get into the system
(the forgot-password email is useless to a locked account). With an empty
dataset those cards render an empty state, so the unlock / issue-temporary-
password controls are never actually exercised.

This command creates a realistic set of locked accounts so those controls can
be demonstrated and tested:

    python manage.py seed_locked_accounts
    python manage.py seed_locked_accounts --count 15
    python manage.py purge_locked_accounts

Safety rules, because this writes rows into a real account table:

- It only ever CREATES accounts. It never touches an account that already
  exists, so re-running it can never lock or unlock a genuine employee or
  student. An existing email is reported and skipped.
- Every seeded account is marked ``must_change_password`` and given a random
  unusable password, so a seeded address can never be signed into. These rows
  exist to be recovered, not to be used.
- The email addresses are on the reserved ``.invalid`` TLD (RFC 2606) and are
  prefixed so they are obvious in a roster, which means no mail is ever
  deliverable to them.
- ``purge_locked_accounts`` removes only rows carrying the seeded marker, so it
  cannot delete a real person's account.
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.academic_structure.models import AcademicYear
from apps.accounts.account_admin import lock_user_account
from apps.accounts.models import (
    EmployeeProfile,
    Role,
    RoleAssignment,
    SexChoices,
    StudentProfile,
    User,
)

# Marker so the purge command can tell a seeded row from a real one without
# relying on the email pattern alone.
SEED_EMAIL_PREFIX = "locked.sample."
SEED_EMAIL_DOMAIN = "@locked.invalid"

# (first, middle, last, employee_number or LRN, role_code, kind)
# `kind` is "staff" or "student" and decides which profile model is created.
SAMPLE_LOCKED_ACCOUNTS = [
    ("Marisol", "D", "Agoncillo", "EMP-9001", "teacher", "staff"),
    ("Roderick", "P", "Balingit", "EMP-9002", "teacher", "staff"),
    ("Evangeline", "S", "Cordero", "EMP-9003", "registrar", "staff"),
    ("Norberto", "A", "Dela Cruz", "EMP-9004", "assistant_principal", "staff"),
    ("Kristine", "M", "Enriquez", "EMP-9005", "principal", "staff"),
    ("Joel", "R", "Fajardo", "EMP-9006", "teacher", "staff"),
    ("Perlita", "T", "Guinto", "EMP-9007", "teacher", "staff"),
    ("Ansel", "B", "Hidalgo", "EMP-9008", "registrar", "staff"),
    ("Carmela", "N", "Ilagan", "EMP-9009", "teacher", "staff"),
    ("Rodel", "V", "Jumero", "EMP-9010", "teacher", "staff"),
    ("Divina", "C", "Lacorte", "EMP-9011", "teacher", "staff"),
    ("Benigno", "R", "Magsaysay", "EMP-9012", "teacher", "staff"),
    ("Imelda", "L", "Nepomuceno", "EMP-9013", "assistant_principal", "staff"),
    ("Teodoro", "L", "Ocampo", "EMP-9014", "teacher", "staff"),
    ("Josefina", "P", "Pangilinan", "000000009001", "student", "student"),
    ("Rogelio", "D", "Quiambao", "000000009002", "student", "student"),
    ("Ernestina", "A", "Roxas", "000000009003", "student", "student"),
    ("Nestor", "J", "Serrano", "000000009004", "student", "student"),
    ("Leonor", "K", "Tolentino", "000000009005", "student", "student"),
    ("Ferdinand", "G", "Uy", "000000009006", "student", "student"),
]

# Vary the lock time backwards from now so the card's "when" column shows a
# spread of dates rather than every row stamped with the same second.
LOCK_MINUTES_AGO = 35


def seed_email(first_name, last_name):
    slug = "{}.{}".format(first_name, last_name).lower()
    return "{}{}{}".format(SEED_EMAIL_PREFIX, slug, SEED_EMAIL_DOMAIN)


class Command(BaseCommand):
    help = "Create locked sample accounts so the admin recovery UI has real rows"

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=len(SAMPLE_LOCKED_ACCOUNTS),
            help="How many sample locked accounts to ensure exist (default: %(default)s)",
        )

    def handle(self, *args, **options):
        target = max(int(options.get("count") or 0), 0)
        if not target:
            self.stdout.write(self.style.WARNING("Nothing to do (--count 0)."))
            return

        ay = AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).first()
        if not ay:
            self.stdout.write(
                self.style.ERROR(
                    "No active Academic Year found. Create one before seeding, because a "
                    "role assignment cannot exist without one."
                )
            )
            return

        samples = SAMPLE_LOCKED_ACCOUNTS[:target]
        created_count = skipped_count = 0

        for index, (first, middle, last, number, role_code, kind) in enumerate(samples):
            email = seed_email(first, last)
            role = Role.objects.filter(code=role_code).first()
            if role is None:
                self.stdout.write(
                    self.style.WARNING(
                        "Skipping {}: role '{}' does not exist. Run create_role_groups "
                        "or assign_role_permissions first.".format(email, role_code)
                    )
                )
                skipped_count += 1
                continue

            if User.objects.filter(email=email).exists():
                # Never re-touch an existing row: this command creates, it does
                # not reconcile, so a genuine account with the same address would
                # otherwise be forced into a locked state.
                skipped_count += 1
                continue

            user = User.objects.create(
                email=email,
                status=User.Status.ACTIVE,
                must_change_password=True,
            )
            # An unusable password. These accounts exist so an administrator can
            # practise recovering them; there must be no way to sign in as one.
            user.set_unusable_password()
            user.save(update_fields=["password"])

            if kind == "student":
                StudentProfile.objects.create(
                    user=user,
                    lrn=number,
                    first_name=first,
                    middle_initial=middle,
                    last_name=last,
                    birthdate=date(2011, 5, 14),
                    sex=SexChoices.FEMALE,
                )
            else:
                EmployeeProfile.objects.create(
                    user=user,
                    employee_number=number,
                    first_name=first,
                    middle_initial=middle,
                    last_name=last,
                )

            RoleAssignment.objects.get_or_create(
                user=user, role=role, academic_year=ay
            )

            # Spread the lock times out so the card's "when" column is readable.
            user.locked_at = timezone.now() - timedelta(
                minutes=LOCK_MINUTES_AGO * (index + 1)
            )
            user.failed_login_attempts = 5
            user.save(update_fields=["locked_at", "failed_login_attempts", "status", "is_active"])

            # Go through the real lock helper rather than assigning the status
            # directly, so the seeded rows carry the same ActivityLog entry a
            # genuine lockout does.
            lock_user_account(
                user,
                reason="Seeded sample account for recovery testing",
            )
            created_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                "Locked sample accounts: {} created, {} skipped. "
                "{} locked account(s) in total.".format(
                    created_count,
                    skipped_count,
                    User.objects.filter(status=User.Status.LOCKED).count(),
                )
            )
        )
        self.stdout.write(
            "Review them under Security Center > Locked Accounts, or remove them with "
            "'python manage.py purge_locked_accounts'."
        )