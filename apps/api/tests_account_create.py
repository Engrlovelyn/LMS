"""Tests for the single-employee creation endpoint.

Covers the failures reported from the dashboard: a new employee never reached
PostgreSQL, and no verification mail ever arrived.
"""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import (
    EmailVerificationToken,
    EmployeeProfile,
    Role,
    RoleAssignment,
    User,
)
from apps.academic_structure.models import AcademicYear


class AccountCreateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026-2027", start_date="2026-08-01", end_date="2027-03-31",
            status=AcademicYear.STATUS_ACTIVE,
        )
        for code in (Role.Code.TEACHER, Role.Code.PRINCIPAL):
            Role.objects.get_or_create(code=code)
        cls.admin = User.objects.create_user(
            email="admin@test.local", password="AdminPass123!", is_superuser=True
        )
        cls.admin.status = User.Status.ACTIVE
        cls.admin.save()

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("api_account_create")

    def payload(self, **overrides):
        data = {
            "name": "Emelito Caleda Malanot",
            "email": "new.teacher@test.local",
            "emp": "EMP-2026-001",
            "role": "Teacher",
            "contact": "09171234567",
        }
        data.update(overrides)
        return data

    def post(self, **overrides):
        return self.client.post(self.url, data=self.payload(**overrides))

    # -- persistence ------------------------------------------------------
    def test_creates_user_profile_and_role_in_database(self):
        with mock.patch(
            "apps.accounts.email_branding.send_html_email", return_value=1
        ):
            response = self.post()

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["ok"])

        user = User.objects.get(email="new.teacher@test.local")
        self.assertEqual(user.status, User.Status.PENDING_VERIFICATION)
        self.assertFalse(user.is_active, "unverified accounts must not be active")

        profile = EmployeeProfile.objects.get(user=user)
        self.assertEqual(profile.employee_number, "EMP-2026-001")
        self.assertEqual(profile.first_name, "Emelito")
        self.assertEqual(profile.last_name, "Malanot")
        self.assertEqual(profile.contact_number, "09171234567")

        self.assertTrue(
            RoleAssignment.objects.filter(
                user=user, academic_year=self.year, role__code=Role.Code.TEACHER
            ).exists()
        )

    def test_returns_usable_temporary_password(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post().json()
        temp = body["temp_password"]
        self.assertTrue(temp)
        self.user = User.objects.get(email="new.teacher@test.local")
        self.client.force_login(User.objects.get(email="admin@test.local"))
        self.assertTrue(self.user.check_password(temp))

    def test_multiple_roles_are_assigned(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post(roles=["Teacher", "Principal"]).json()
        self.assertEqual(sorted(body["roles"]), sorted(
            [Role.Code.TEACHER, Role.Code.PRINCIPAL]
        ))
        user = User.objects.get(email="new.teacher@test.local")
        self.assertEqual(
            RoleAssignment.objects.filter(
                user=user, academic_year=self.year
            ).count(),
            2,
        )

    # -- verification mail ------------------------------------------------
    def test_sends_one_verification_token_and_reports_success(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post().json()
        self.assertTrue(body["email_sent"])
        user = User.objects.get(email="new.teacher@test.local")
        self.assertEqual(
            EmailVerificationToken.objects.filter(
                user=user, verified_at__isnull=True
            ).count(),
            1,
        )

    def test_account_is_still_saved_when_the_email_fails(self):
        with mock.patch(
            "apps.accounts.email_branding.send_html_email",
            side_effect=RuntimeError("Email delivery failed: SMTP rejected (535 Invalid credentials)"),
        ):
            response = self.post()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["email_sent"])
        self.assertIn("email_error", body)
        # The row must exist, and the admin must be able to hand the password
        # over manually rather than the account being stranded.
        self.assertTrue(User.objects.filter(email="new.teacher@test.local").exists())
        self.assertTrue(body["temp_password"])

    # -- validation -------------------------------------------------------
    def test_rejects_invalid_email_with_field_error(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post(email="not-an-email").json()
        self.assertIn("email", body["errors"])
        self.assertFalse(User.objects.filter(email="not-an-email").exists())

    def test_rejects_duplicate_email(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            self.post()
            body = self.post().json()
        self.assertIn("already exists", body["errors"]["email"])
        self.assertEqual(User.objects.filter(email="new.teacher@test.local").count(), 1)

    def test_rejects_duplicate_employee_number(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            self.post()
            body = self.post(email="other@test.local").json()
        self.assertIn("already in use", body["errors"]["employee_number"])

    def test_rejects_overlong_employee_number_without_creating_user(self):
        """A 30-char ID used to pass the browser and then 500 in Postgres."""
        long_id = "SVI-EMP-2026-0001-STAFF"
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post(emp=long_id).json()
        self.assertIn("employee_number", body["errors"])
        self.assertIn("15 characters or fewer", body["errors"]["employee_number"])
        # The critical part: no orphan User row left behind.
        self.assertFalse(User.objects.filter(email="new.teacher@test.local").exists())

    def test_rejects_bad_contact_number(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post(contact="12345").json()
        self.assertIn("contact", body["errors"])

    def test_requires_a_role(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post(role="").json()
        self.assertIn("role", body["errors"])

    def test_rejects_unknown_role(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post(role="Wizard").json()
        self.assertIn("role", body["errors"])

    def test_requires_first_and_last_name(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post(name="Cher").json()
        self.assertIn("first_name", body["errors"])
        self.assertFalse(User.objects.filter(email="new.teacher@test.local").exists())

    def test_parse_name_never_duplicates_a_single_token(self):
        from apps.api.views import _parse_name

        first, _mi, last = _parse_name("Cher")
        self.assertNotEqual(
            (first, last), ("Cher", "Cher"),
            "a one-word name must not be written to both name columns",
        )
        self.assertEqual(last, "Cher")

    def test_non_admin_cannot_create(self):
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
        self.assertFalse(User.objects.filter(email="new.teacher@test.local").exists())


    # -- staff demographics -------------------------------------------------
    # sex/birthdate feed the Staff Demographics panel, so they must survive the
    # create call and reject nonsense instead of writing it to the profile.
    def test_sex_and_birthdate_are_stored(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post(sex="F", birthdate="1990-05-04").json()
        self.assertTrue(body["ok"])
        profile = EmployeeProfile.objects.get(user__email="new.teacher@test.local")
        self.assertEqual(profile.sex, "F")
        self.assertEqual(profile.birthdate.isoformat(), "1990-05-04")

    def test_long_form_sex_and_blank_birthdate(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            body = self.post(sex="Male").json()
        self.assertTrue(body["ok"])
        profile = EmployeeProfile.objects.get(user__email="new.teacher@test.local")
        self.assertEqual(profile.sex, "M")
        self.assertIsNone(profile.birthdate)

    def test_unknown_sex_is_rejected(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            response = self.post(sex="Robot")
        self.assertEqual(response.status_code, 400)
        self.assertIn("sex", response.json()["errors"])
        self.assertFalse(User.objects.filter(email="new.teacher@test.local").exists())

    def test_future_birthdate_is_rejected(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            response = self.post(sex="F", birthdate="2999-01-01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("birthdate", response.json()["errors"])

    def test_malformed_birthdate_is_rejected(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            response = self.post(birthdate="not-a-date")
        self.assertEqual(response.status_code, 400)
        self.assertIn("birthdate", response.json()["errors"])


class ResendVerificationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026-2027", start_date="2026-08-01", end_date="2027-03-31",
            status=AcademicYear.STATUS_ACTIVE,
        )
        Role.objects.get_or_create(code=Role.Code.SYSTEM_ADMIN)
        cls.admin = User.objects.create_user(
            email="admin2@test.local", password="AdminPass123!", is_superuser=True
        )
        cls.admin.status = User.Status.ACTIVE
        cls.admin.save()
        cls.pending = User.objects.create_user(
            email="pending@test.local", password="OldPass123!"
        )
        cls.pending.status = User.Status.PENDING_VERIFICATION
        cls.pending.save()
        cls.active = User.objects.create_user(
            email="active@test.local", password="LivePass123!"
        )
        cls.active.status = User.Status.ACTIVE
        cls.active.save()

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse("api_account_resend_verification")

    def test_resend_issues_new_password_and_mail(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            response = self.client.post(self.url, data={"email": "pending@test.local"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["email_sent"])

        self.pending.refresh_from_db()
        self.assertFalse(self.pending.check_password("OldPass123!"))
        self.assertTrue(
            User.objects.get(email="pending@test.local").is_active is False
        )

    def test_resend_supersedes_stale_links(self):
        stale = EmailVerificationToken.objects.create(
            user=self.pending, token="stale-token",
            expires_at="2099-01-01T00:00:00Z",
        )
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            self.client.post(self.url, data={"email": "pending@test.local"})
        stale.refresh_from_db()
        self.assertIsNotNone(stale.verified_at)

    def test_resend_reports_transport_failure_with_password(self):
        with mock.patch(
            "apps.accounts.email_branding.send_html_email",
            side_effect=RuntimeError("Email delivery failed: SMTP rejected (535 Invalid credentials)"),
        ):
            response = self.client.post(
                self.url, data={"email": "pending@test.local"}
            )
        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertFalse(body["email_sent"])
        self.assertTrue(body["temp_password"])

    def test_cannot_resend_for_verified_account(self):
        response = self.client.post(self.url, data={"email": "active@test.local"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("already verified", response.json()["message"])

    def test_unknown_email_is_404(self):
        response = self.client.post(self.url, data={"email": "ghost@test.local"})
        self.assertEqual(response.status_code, 404)
