import os

from django.core.management.base import BaseCommand
from apps.academic_structure.models import AcademicYear
from apps.accounts.models import User, Role, RoleAssignment, EmployeeProfile, StudentProfile


# One shared demo password so support can tell staff a single credential. The
# value lives in ``DEMO_ACCOUNT_PASSWORD`` so it is not hardcoded in git; the
# fallback keeps a known, clearly-vanilla default only so a fresh developer
# checkout still boots. Do not use this default on any real deployment.
def _demo_password():
    return os.getenv("DEMO_ACCOUNT_PASSWORD", "tass-demo-2026")


ACCOUNTS = [
    # (first_name, last_name, email, role_code, employee_number)
    ("Lovelyn", "Malanot", "lovelynmalanot@gmail.com", "teacher", "EMP-0001"),
    ("Cherry", "Javier", "cherryjavier@gmail.com", "principal", "EMP-0002"),
    ("Dina", "Respicio", "dinarespicio@gmail.com", "assistant_principal", "EMP-0003"),
    ("Jay Ar", "Antonio", "jayarantonio@gmail.com", "registrar", "EMP-0004"),
    ("Vince", "Arnedo", "vincearnedo@gmail.com", "system_admin", "EMP-0005"),
    ("Lomelyn", "Callueng", "lomelyncallueng@gmail.com", "quality_audit", "EMP-0006"),
    ("Elena", "Mendoza", "elenamendoza@gmail.com", "elementary_principal", "EMP-0007"),
]

STUDENT = ("Khaizer", "Reyes", "khaizerreyes@gmail.com", "000000000001")


class Command(BaseCommand):
    help = "Seed the 8 official demo accounts (one per role) with a shared demo password"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-existing",
            action="store_true",
            help="Also reset the password of accounts that already exist. "
            "Without this flag existing accounts are left untouched, so re-running "
            "the seed on a real database cannot wipe a live administrator's password.",
        )

    def handle(self, *args, **options):
        ay = AcademicYear.objects.filter(status="ACTIVE").first()
        if not ay:
            self.stdout.write(self.style.ERROR("No active Academic Year found. Create one first."))
            return

        password = _demo_password()
        reset = options.get("reset_existing", False)

        for first, last, email, role_code, emp_no in ACCOUNTS:
            role = Role.objects.get(code=role_code)
            user, created = User.objects.get_or_create(
                email=email,
                defaults={"status": User.Status.ACTIVE},
            )
            changed_password = False
            if created:
                user.set_password(password)
                user.status = User.Status.ACTIVE
                user.failed_login_attempts = 0
                user.save()
                changed_password = True
            elif reset:
                user.set_password(password)
                user.status = User.Status.ACTIVE
                user.failed_login_attempts = 0
                user.save()
                changed_password = True

            if created:
                self.stdout.write(self.style.SUCCESS(f"Created user: {email}"))
            elif changed_password:
                self.stdout.write(f"Reset password: {email}")
            else:
                self.stdout.write(f"Exists (password untouched): {email}")

            RoleAssignment.objects.get_or_create(user=user, role=role, academic_year=ay)

            # The employee number is unique and the target database may already
            # hold a profile for that number under a different user (the seed has
            # run against both sample and real data over this project's life).
            # Bind a profile to this user whenever possible and never steal or
            # overwrite an existing one.
            profile = EmployeeProfile.objects.filter(user=user).first()
            if profile is None:
                number = emp_no
                if EmployeeProfile.objects.filter(employee_number=number).exists():
                    base, seq = number, 1
                    while EmployeeProfile.objects.filter(employee_number=number).exists():
                        seq += 1
                        number = f"{base}-{seq}"
                profile = EmployeeProfile.objects.create(
                    user=user,
                    employee_number=number,
                    first_name=first,
                    last_name=last,
                )

        # Student
        first, last, email, lrn = STUDENT
        role = Role.objects.get(code="student")
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"status": User.Status.ACTIVE},
        )
        if created or reset:
            user.set_password(password)
            user.status = User.Status.ACTIVE
            user.failed_login_attempts = 0
            user.save()
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created user: {email}"))
        elif reset:
            self.stdout.write(f"Reset password: {email}")
        else:
            self.stdout.write(f"Exists (password untouched): {email}")

        RoleAssignment.objects.get_or_create(user=user, role=role, academic_year=ay)

        StudentProfile.objects.get_or_create(
            user=user,
            defaults={
                "lrn": lrn,
                "first_name": first,
                "last_name": last,
                "birthdate": "2008-01-01",
            },
        )

        self.stdout.write(self.style.SUCCESS("All 8 official accounts seeded."))