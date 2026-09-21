from django.core.management.base import BaseCommand
from apps.academic_structure.models import AcademicYear
from apps.accounts.models import User, Role, RoleAssignment, EmployeeProfile, StudentProfile


ACCOUNTS = [
    # (first_name, last_name, email, password, role_code, employee_number)
    ("Lovelyn", "Malanot", "lovelynmalanot@gmail.com", "lovelyn12345", "teacher", "EMP-0001"),
    ("Cherry", "Javier", "cherryjavier@gmail.com", "cherry12345", "principal", "EMP-0002"),
    ("Dina", "Respicio", "dinarespicio@gmail.com", "dina12345", "assistant_principal", "EMP-0003"),
    ("Jay Ar", "Antonio", "jayarantonio@gmail.com", "jayar12345", "registrar", "EMP-0004"),
    ("Vince", "Arnedo", "vincearnedo@gmail.com", "vince12345", "system_admin", "EMP-0005"),
    ("TASS", "Administrator", "tass.admin@tass.edu.ph", "tassadmin12345", "tass_administrator", "EMP-0006"),
]

STUDENT = ("Khaizer", "Reyes", "khaizerreyes@gmail.com", "khaizer12345", "000000000001")


class Command(BaseCommand):
    help = "Seed the 7 official demo accounts (one per role) with known passwords"

    def handle(self, *args, **kwargs):
        ay = AcademicYear.objects.filter(status="ACTIVE").first()
        if not ay:
            self.stdout.write(self.style.ERROR("No active Academic Year found. Create one first."))
            return

        for first, last, email, password, role_code, emp_no in ACCOUNTS:
            role = Role.objects.get(code=role_code)
            user, created = User.objects.get_or_create(
                email=email,
                defaults={"status": User.Status.ACTIVE},
            )
            user.set_password(password)
            user.status = User.Status.ACTIVE
            user.failed_login_attempts = 0
            user.save()
            if created:
                self.stdout.write(self.style.SUCCESS(f"Created user: {email}"))
            else:
                self.stdout.write(f"Reset credentials: {email}")

            RoleAssignment.objects.get_or_create(user=user, role=role, academic_year=ay)

            EmployeeProfile.objects.get_or_create(
                user=user,
                defaults={
                    "employee_number": emp_no,
                    "first_name": first,
                    "last_name": last,
                },
            )

        # Student
        first, last, email, password, lrn = STUDENT
        role = Role.objects.get(code="student")
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"status": User.Status.ACTIVE},
        )
        user.set_password(password)
        user.status = User.Status.ACTIVE
        user.failed_login_attempts = 0
        user.save()
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created user: {email}"))
        else:
            self.stdout.write(f"Reset credentials: {email}")

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

        self.stdout.write(self.style.SUCCESS("All 7 official accounts seeded."))
