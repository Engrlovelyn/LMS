"""Tests for the single-student creation endpoint.

The System Administrator "Add User" modal previously only produced employees,
so a learner account could only be created by the registrar's bulk roster
import. These cover the new single-student path: a real User + StudentProfile +
Student RoleAssignment, the generated temporary password, and the validation
that keeps a bad LRN or a missing birthdate from reaching Postgres.
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import (
    EmailVerificationToken,
    Role,
    RoleAssignment,
    StudentProfile,
    User,
)
from apps.academic_structure.models import AcademicYear


class StudentCreateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026-2027", start_date="2026-08-01", end_date="2027-03-31",
            status=AcademicYear.STATUS_ACTIVE,
        )
        for code in (Role.Code.STUDENT, Role.Code.TEACHER):
            Role.objects.get_or_create(code=code)
        cls.admin = User.objects.create_user(
            email="admin@test.local", password="AdminPass123!", is_superuser=True
        )
        cls.admin.status = User.Status.ACTIVE
        cls.admin.save()

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("api_student_create")

    def payload(self, **overrides):
        data = {
            "name": "Juan Dela Cruz",
            "first_name": "Juan",
            "last_name": "Dela Cruz",
            "email": "new.student@test.local",
            "lrn": "107632190045",
            "birthdate": "2012-05-04",
            "sex": "M",
        }
        data.update(overrides)
        return data

    def post(self, **overrides):
        return self.client.post(self.url, data=self.payload(**overrides))

    # -- persistence ------------------------------------------------------
    def test_creates_user_profile_and_student_role_in_database(self):
        with mock.patch(
            "apps.accounts.email_branding.send_html_email", return_value=1
        ):
            response = self.post()

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["lrn"], "107632190045")
        self.assertEqual(body["roles"], ["student"])

        user = User.objects.get(email="new.student@test.local")
        self.assertEqual(user.status, User.Status.PENDING_VERIFICATION)
        self.assertFalse(user.is_active, "unverified accounts must not be active")

        profile = StudentProfile.objects.get(user=user)
        self.assertEqual(profile.lrn, "107632190045")
        self.assertEqual(profile.first_name, "Juan")
        self.assertEqual(profile.last_name, "Dela Cruz")
        self.assertEqual(profile.birthdate.isoformat(), "2012-05-04")
        self.assertEqual(profile.sex, "M")

        self.assertTrue(
            RoleAssignment.objects.filter(
                user=user, academic_year=self.year, role__code=Role.Code.STUDENT
            ).exists()
        )

    def test_returns_usable_temporary_password(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post().json()
        temp = body["temp_password"]
        self.assertTrue(temp)
        user = User.objects.get(email="new.student@test.local")
        self.assertTrue(user.check_password(temp))

    def test_optional_learner_fields_are_stored(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            self.post(
                address="12 Mabini St, San Vicente",
                guardian_name="Maria Dela Cruz",
                guardian_email="maria@test.local",
                guardian_contact="09181234567",
                contact="09171234567",
            )
        profile = StudentProfile.objects.get(user__email="new.student@test.local")
        self.assertEqual(profile.address, "12 Mabini St, San Vicente")
        self.assertEqual(profile.guardian_name, "Maria Dela Cruz")
        self.assertEqual(profile.guardian_email, "maria@test.local")
        self.assertEqual(profile.guardian_contact_number, "09181234567")
        self.assertEqual(profile.contact_number, "09171234567")

    # -- verification mail ------------------------------------------------
    def test_sends_one_verification_token_and_reports_success(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post().json()
        self.assertTrue(body["email_sent"])
        user = User.objects.get(email="new.student@test.local")
        self.assertEqual(
            EmailVerificationToken.objects.filter(
                user=user, verified_at__isnull=True
            ).count(),
            1,
        )

    def test_account_is_still_saved_when_the_email_fails(self):
        with mock.patch(
            "apps.accounts.email_branding.send_html_email",
            side_effect=RuntimeError("Email delivery failed: SMTP rejected (535)"),
        ):
            response = self.post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["email_sent"])
        self.assertIn("email_error", body)
        self.assertTrue(
            User.objects.filter(email="new.student@test.local").exists()
        )
        self.assertTrue(body["temp_password"])

    # -- validation -------------------------------------------------------
    def test_rejects_lrn_that_is_not_twelve_digits(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            response = self.post(lrn="12345")
        self.assertEqual(response.status_code, 400)
        self.assertIn("lrn", response.json()["errors"])
        self.assertFalse(
            User.objects.filter(email="new.student@test.local").exists()
        )

    def test_rejects_duplicate_lrn(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            self.post()
            body = self.post(email="other@test.local").json()
        self.assertIn("already in use", body["errors"]["lrn"])

    def test_rejects_duplicate_email(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            self.post()
            body = self.post(lrn="107632190099").json()
        self.assertIn("already exists", body["errors"]["email"])

    def test_birthdate_is_required(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            response = self.post(birthdate="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("birthdate", response.json()["errors"])
        self.assertFalse(
            User.objects.filter(email="new.student@test.local").exists()
        )

    def test_future_birthdate_is_rejected(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            response = self.post(birthdate="2999-01-01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("birthdate", response.json()["errors"])

    def test_malformed_guardian_email_is_rejected(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            response = self.post(guardian_email="not-an-email")
        self.assertEqual(response.status_code, 400)
        self.assertIn("guardian_email", response.json()["errors"])

    def test_requires_first_and_last_name(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            response = self.post(name="Cher", first_name="", last_name="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("first_name", response.json()["errors"])

    def test_non_registrar_cannot_create(self):
        teacher = User.objects.create_user(
            email="t@test.local", password="TeachPass123!"
        )
        RoleAssignment.objects.get_or_create(
            user=teacher,
            role=Role.objects.get(code=Role.Code.TEACHER),
            academic_year=self.year,
        )
        teacher = User.objects.get(pk=teacher.pk)
        teacher.status = User.Status.ACTIVE
        teacher.save(update_fields=["status", "is_active"])
        self.client.force_login(teacher)
        response = self.post()
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            User.objects.filter(email="new.student@test.local").exists()
        )
