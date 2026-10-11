"""Purge demo-derived records from the database.

Removes accounts/data that were created when browsers synced the legacy
front-end demo arrays to the API (e.g. ``/api/students/``, ``/api/accounts/``)
before those arrays were neutralized behind ``window.__TASS_REAL__``.

Targets:
* Student accounts whose LRN matches the known demo LRN set.
* Self-serve accounts (User + EmployeeProfile) whose email matches the known
  demo email set from the system_admin demo roster.

Deletes dependents first so PROTECT constraints are satisfied, then the
StudentProfile/User rows themselves. Idempotent and safe to rerun.

Run without ``--commit`` to preview deletion counts first.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import EmployeeProfile, StudentProfile, User

DEMO_LRNS = [
    "136100100001", "136100100002", "136100100003", "136100100004",
    "136100100005", "136100100006", "136100100007", "136100100008",
    "136100100009", "136100100010", "136100100011", "136100100012",
    "136100100013", "136100100014", "136100100015", "136100100016",
    "136100100017", "136100100018", "136100100021",
    "136410050101", "136410050102", "136410050103", "136410050104",
    "136410050105", "136410050106", "136410050107", "136410050108",
]

DEMO_ACCOUNT_EMAILS = [
    "lovelyn.malanot@sanvicente.edu.ph",
    "jose.ramirez@sanvicente.edu.ph",
    "gina.aguilar@sanvicente.edu.ph",
    "norma.villanueva@sanvicente.edu.ph",
    "ben.castillo@sanvicente.edu.ph",
    "carlo.fernandez@sanvicente.edu.ph",
    "helena.rodriguez@sanvicente.edu.ph",
    "maria.delacruz@sanvicente.edu.ph",
    "alex.ramos@sanvicente.edu.ph",
    "juan.delapena@sanvicente.edu.ph",
]


class Command(BaseCommand):
    help = "Purge demo-derived student/account records from the database"

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Actually delete the records. Without this flag, only a preview is printed.",
        )

    def handle(self, *args, **options):
        commit = options["commit"]

        student_profiles = StudentProfile.objects.filter(lrn__in=DEMO_LRNS)

        demo_emails = set(DEMO_ACCOUNT_EMAILS)
        demo_emails.update(
            sp.user.email
            for sp in student_profiles.select_related("user")
            if sp.user and sp.user.email
        )
        demo_email_users = list(User.objects.filter(email__in=demo_emails))
        demo_email_users_by_id = {u.pk: u for u in demo_email_users}

        student_user_ids = set()
        for sp in student_profiles.select_related("user"):
            if sp.user:
                student_user_ids.add(sp.user_id)
        student_users = list(User.objects.filter(pk__in=student_user_ids))

        # Merge users referenced from both paths.
        all_users = list({u.pk: u for u in student_users + demo_email_users}.values())

        allowed_employee_ids = set(
            EmployeeProfile.objects.filter(user_id__in=student_user_ids)
            .values_list("pk", flat=True)
        )
        allowed_employee_ids.update(
            EmployeeProfile.objects.filter(
                user__in=all_users, user__email__in=demo_emails
            ).values_list("pk", flat=True)
        )

        demo_students = student_profiles.values_list("pk", flat=True)

        dependent_models = []

        def register(name, qs):
            count = qs.count()
            if count:
                dependent_models.append((name, count))
            return qs

        # ── Student-dependent records (PROTECT -> must delete first) ──
        if demo_students:
            register(
                "attendance records",
                self._related("attendance.AttendanceRecord", "student_id", demo_students),
            )
            register(
                "student grades",
                self._related("assessments.StudentGrade", "student_id", demo_students),
            )
            register(
                "submissions",
                self._related("assessments.Submission", "student_id", demo_students),
            )
            register(
                "assessment notifications",
                self._related("assessments.AssessmentNotification", "student_id", demo_students),
            )
            register(
                "VC enrollments",
                self._related(
                    "virtual_classrooms.VirtualClassroomEnrollment",
                    "student_id",
                    demo_students,
                ),
            )
            register(
                "student enrollments",
                self._related(
                    "academic_structure.StudentEnrollment", "student_id", demo_students
                ),
            )

        # ── User-dependent records ──
        user_ids = [u.pk for u in all_users] if all_users else []
        if user_ids:
            register(
                "email verification tokens",
                self._related("accounts.EmailVerificationToken", "user_id", user_ids),
            )
            register(
                "password reset tokens",
                self._related("accounts.PasswordResetToken", "user_id", user_ids),
            )
            register(
                "login sessions",
                self._related("accounts.LoginSession", "user_id", user_ids),
            )
            register(
                "password history",
                self._related("accounts.PasswordHistory", "user_id", user_ids),
            )
            register(
                "role assignments",
                self._related("accounts.RoleAssignment", "user_id", user_ids),
            )
            register(
                "activity logs",
                self._related("accounts.ActivityLog", "user_id", user_ids),
            )

        total_dependents = sum(n for _, n in dependent_models)
        student_count = student_profiles.count()
        user_count = len(all_users)
        employee_count = len(allowed_employee_ids)

        self.stdout.write(self.style.MIGRATE_HEADING("Demo data purge preview"))
        self.stdout.write(f"Demo LRN match count : {student_count}")
        self.stdout.write(f"Demo-email users      : {len(demo_email_users)}")
        self.stdout.write(f"Total user targets    : {user_count}")
        self.stdout.write(f"Employee profiles     : {employee_count}")
        for name, count in dependent_models:
            self.stdout.write(f"  dependent {name:<24}: {count}")

        if not commit:
            self.stdout.write(
                self.style.WARNING(
                    "Preview only. Re-run with --commit to delete "
                    f"{total_dependents} dependent record(s), "
                    f"{employee_count} employee profile(s), "
                    f"{student_count} student profile(s), "
                    f"{user_count} user(s)."
                )
            )
            return

        with transaction.atomic():
            self._delete_related_records(demo_students, user_ids)
            EmployeeProfile.objects.filter(
                pk__in=list(allowed_employee_ids) if allowed_employee_ids else []
            ).delete()
            student_profiles.delete()
            User.objects.filter(pk__in=[u.pk for u in all_users]).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Purged {student_count} student(s), "
                f"{employee_count} employee profile(s), "
                f"{user_count} user(s), and {total_dependents} dependent record(s)."
            )
        )

    def _related(self, model_path, field_name, ids):
        from django.apps import apps

        model = apps.get_model(model_path)
        return model.objects.filter(**{field_name + "__in": list(ids)})

    def _delete_related_records(self, demo_student_ids, user_ids):
        from django.apps import apps

        targets = [
            (
                "attendance.AttendanceRecord",
                "student_id",
                demo_student_ids,
            ),
            ("assessments.StudentGrade", "student_id", demo_student_ids),
            ("assessments.Submission", "student_id", demo_student_ids),
            (
                "assessments.AssessmentNotification",
                "student_id",
                demo_student_ids,
            ),
            (
                "virtual_classrooms.VirtualClassroomEnrollment",
                "student_id",
                demo_student_ids,
            ),
            (
                "academic_structure.StudentEnrollment",
                "student_id",
                demo_student_ids,
            ),
            ("accounts.EmailVerificationToken", "user_id", user_ids),
            ("accounts.PasswordResetToken", "user_id", user_ids),
            ("accounts.LoginSession", "user_id", user_ids),
            ("accounts.PasswordHistory", "user_id", user_ids),
            ("accounts.RoleAssignment", "user_id", user_ids),
            ("accounts.ActivityLog", "user_id", user_ids),
        ]
        for model_path, field, ids in targets:
            if not ids:
                continue
            model = apps.get_model(model_path)
            model.objects.filter(**{field + "__in": list(ids)}).delete()