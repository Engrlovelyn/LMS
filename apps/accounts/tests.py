import json
from unittest import mock

from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.academic_structure.models import AcademicYear

from .models import EmailVerificationToken, User
from .views import _send_verification_email


class VerificationEmailResultTests(TestCase):
    """Verification-mail sending must report failures instead of swallowing them."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="new@test.local", password="TempPass123!"
        )
        self.user.status = User.Status.PENDING_VERIFICATION
        self.user.save()
        self.request = RequestFactory().get("/")

    def test_success_returns_ok_and_token(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1):
            result = _send_verification_email(self.user, self.request, password="Temp123!")
        self.assertTrue(result["ok"])
        self.assertEqual(result["user"], self.user)
        self.assertTrue(result["token"])

    def test_failure_returns_error_message(self):
        with mock.patch(
            "apps.accounts.email_branding.send_html_email",
            side_effect=RuntimeError("Email delivery failed: SMTP rejected (535 Invalid credentials)"),
        ):
            result = _send_verification_email(self.user, self.request, password="Temp123!")
        self.assertFalse(result["ok"])
        self.assertIn("Email delivery failed", result["message"])


class AdminUserCreationSendsVerificationEmailTests(TestCase):
    """Creating a User via the Django admin must send a verification email."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            email="root@test.local", password="pass12345x"
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_admin_add_creates_token_and_sends_email(self):
        with mock.patch("apps.accounts.email_branding.send_html_email", return_value=1) as send:
            resp = self.client.post(
                reverse("admin:accounts_user_add"),
                {
                    "email": "fred@test.local",
                    "status": User.Status.PENDING_VERIFICATION,
                    "password1": "TempPass123!",
                    "password2": "TempPass123!",
                    "_save": "Save",
                },
            )
        self.assertEqual(resp.status_code, 302)
        token = EmailVerificationToken.objects.filter(user__email="fred@test.local").first()
        self.assertIsNotNone(token)
        self.assertEqual(send.call_count, 1)
        _, kwargs = send.call_args
        self.assertIn("fred@test.local", kwargs["to_email"])

    def test_admin_add_surfaces_send_failure(self):
        with mock.patch(
            "apps.accounts.email_branding.send_html_email",
            side_effect=RuntimeError("Email delivery failed: SMTP rejected (535 Invalid credentials)"),
        ):
            resp = self.client.post(
                reverse("admin:accounts_user_add"),
                {
                    "email": "george@test.local",
                    "status": User.Status.PENDING_VERIFICATION,
                    "password1": "TempPass123!",
                    "password2": "TempPass123!",
                    "_save": "Save",
                },
            )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            User.objects.filter(email="george@test.local").exists()
        )


class ApiWelcomeMailFailureTests(TestCase):
    """POST /api/accounts/ must report how many verification emails failed."""

    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2025-2026",
            start_date="2025-06-01",
            end_date="2026-03-31",
            status=AcademicYear.STATUS_ACTIVE,
        )
        cls.admin = User.objects.create_superuser(
            email="sa@test.local", password="pass12345x", status=User.Status.ACTIVE
        )
        cls.admin.status = User.Status.ACTIVE
        cls.admin.save()

    def setUp(self):
        self.client.force_login(self.admin)

    def test_accounts_endpoint_reports_email_failure(self):
        payload = {
            "accounts": [
                {
                    "email": "jane@test.local",
                    "name": "Jane Doe",
                    "first": "Jane",
                    "surname": "Doe",
                }
            ]
        }
        with mock.patch(
            "apps.accounts.email_branding.send_html_email",
            side_effect=RuntimeError("Email delivery failed: SMTP rejected (535 Invalid credentials)"),
        ):
            resp = self.client.post(
                reverse("api_accounts"),
                data=json.dumps(payload),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["email_failed"], 1)

class AvatarDeliveryTests(TestCase):
    """Avatar URLs must be stable, not presigned URLs that expire.

    R2 is a private bucket, so FieldFile.url is signed with X-Amz-Expires.
    Rendering that into a page made profile photos vanish once the page aged
    past the signature; the page now carries /accounts/avatar/<key>/ instead.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="avatar@test.local", password="TempPass123!"
        )
        self.user.status = User.Status.ACTIVE
        self.user.save()
        from apps.accounts.models import EmployeeProfile

        self.profile = EmployeeProfile.objects.create(
            user=self.user, employee_number="A-001",
            first_name="Vince", last_name="Arnedo",
        )

    def test_context_processor_returns_stable_path_not_a_presigned_url(self):
        self.profile.avatar = "avatars/burger.jpg"
        self.profile.save()
        from apps.accounts.context_processors import _avatar_url

        url = _avatar_url(self.user)
        self.assertEqual(url, "/accounts/avatar/avatars/burger.jpg/")
        # A presigned URL would carry a signature and an expiry.
        self.assertNotIn("X-Amz-Signature", url)
        self.assertNotIn("X-Amz-Expires", url)

    def test_context_processor_empty_without_avatar(self):
        from apps.accounts.context_processors import _avatar_url

        self.assertEqual(_avatar_url(self.user), "")

    def test_redirect_signs_the_key_per_request_and_does_not_cache(self):
        self.client.force_login(self.user)
        with mock.patch(
            "apps.accounts.views.default_storage.url",
            return_value="https://r2.example/avatars/burger.jpg?X-Amz-Signature=abc",
        ) as signed:
            res = self.client.get("/accounts/avatar/avatars/burger.jpg/")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], signed.return_value)
        # The signed target must not be cached, or a stale signature would
        # outlive its own expiry.
        self.assertIn("no-store", res["Cache-Control"])

    def test_redirect_refuses_keys_outside_the_avatar_prefix(self):
        self.client.force_login(self.user)
        for bad in [
            "learning_resources/secret.pdf",
            "../secrets.env",
            "avatars/../../etc/passwd",
            "/etc/passwd",
        ]:
            with self.subTest(name=bad):
                res = self.client.get(f"/accounts/avatar/{bad}/")
                self.assertEqual(res.status_code, 404)

    def test_redirect_requires_login(self):
        res = self.client.get("/accounts/avatar/avatars/burger.jpg/")
        self.assertEqual(res.status_code, 302)
        self.assertIn("/accounts/login/", res["Location"])

    def test_dashboard_renders_the_stable_path(self):
        """Regression: the rendered page must not embed an expiring URL."""
        from django.contrib.auth.models import Group

        group, _ = Group.objects.get_or_create(name="system_admin")
        self.user.groups.add(group)
        self.profile.avatar = "avatars/burger.jpg"
        self.profile.save()

        self.client.force_login(self.user)
        res = self.client.get(reverse("dashboards:ict"))
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn("/accounts/avatar/avatars/burger.jpg/", html)
        self.assertNotIn("X-Amz-Signature", html)
        self.assertNotIn("X-Amz-Expires", html)

    # The same expiry bug was reachable from every dashboard, not just the
    # System Administrator's, so pin all of them.
    DASHBOARDS = [
        ("system_admin", "ict", "staff"),
        ("registrar", "registrar", "staff"),
        ("teacher", "teacher", "staff"),
        ("principal", "principal", "staff"),
        ("assistant_principal", "academic_coordinator", "staff"),
        ("student", "student", "student"),
        ("quality_audit", "quality_audit", "staff"),
    ]

    def test_no_dashboard_embeds_a_presigned_avatar_url(self):
        from datetime import date as _date

        from django.contrib.auth.models import Group
        from apps.accounts.models import EmployeeProfile, StudentProfile

        for index, (role, view_name, kind) in enumerate(self.DASHBOARDS):
            with self.subTest(dashboard=view_name):
                user = User.objects.create_user(
                    email=f"avatar-{view_name}@test.local", password="TempPass123!"
                )
                user.status = User.Status.ACTIVE
                user.save()
                group, _ = Group.objects.get_or_create(name=role)
                user.groups.add(group)
                if kind == "student":
                    person = StudentProfile.objects.create(
                        user=user, lrn=f"900000000{index:03d}",
                        first_name="Ana", last_name="Reyes",
                        birthdate=_date(2010, 1, 1),
                    )
                else:
                    person = EmployeeProfile.objects.create(
                        user=user, employee_number=f"EMP-{index:04d}",
                        first_name="Ana", last_name="Reyes",
                    )
                person.avatar = f"avatars/{view_name}.jpg"
                person.save()

                self.client.force_login(user)
                res = self.client.get(reverse(f"dashboards:{view_name}"))
                self.assertEqual(res.status_code, 200)
                html = res.content.decode()
                self.assertIn(f"/accounts/avatar/avatars/{view_name}.jpg/", html)
                self.assertNotIn("X-Amz-Signature", html)
                self.assertNotIn("X-Amz-Expires", html)


class UserAccountManagementTests(TestCase):
    """View/Update User Account and Reactivate Account screens."""

    @classmethod
    def setUpTestData(cls):
        from apps.accounts.models import EmployeeProfile

        cls.admin = User.objects.create_superuser(
            email="acct_admin@test.local", password="pass12345x"
        )
        cls.admin.status = User.Status.ACTIVE
        cls.admin.save()

        cls.target = User.objects.create_user(
            email="acct_target@test.local", password="pass12345x"
        )
        cls.target.status = User.Status.ACTIVE
        cls.target.save()
        EmployeeProfile.objects.create(
            user=cls.target, employee_number="EMP-2001",
            first_name="Rosa", last_name="Lim",
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_view_user_account_renders(self):
        res = self.client.get(
            reverse("accounts:account_detail", args=[self.target.pk])
        )
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "acct_target@test.local")

    def test_update_user_account_changes_email_and_status(self):
        res = self.client.post(
            reverse("accounts:account_detail", args=[self.target.pk]),
            {"email": "renamed@test.local", "status": User.Status.LOCKED},
        )
        self.assertEqual(res.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.email, "renamed@test.local")
        self.assertEqual(self.target.status, User.Status.LOCKED)

    def test_reactivate_restores_active_status_and_groups(self):
        from django.contrib.auth.models import Group

        group, _ = Group.objects.get_or_create(name="teacher")
        self.target.status = User.Status.ARCHIVED
        self.target.save()
        self.target.groups.clear()

        res = self.client.post(
            reverse("accounts:account_reactivate", args=[self.target.pk])
        )
        self.assertEqual(res.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.status, User.Status.ACTIVE)
