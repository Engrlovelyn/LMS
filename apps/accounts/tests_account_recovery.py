"""Administrator account recovery: unlocking a lockout and issuing a password.

Covers both recovery paths end to end, because the failure mode this replaces
was silent -- the dashboard showed "Password Reset Successfully" while the
account stayed locked in the database.
"""

import json
from datetime import date

from django.contrib.auth import authenticate
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.account_admin import (
    AccountActionError,
    generate_temporary_password,
    issue_temporary_password,
    serialize_locked_account,
    unlock_user_account,
)
from apps.accounts.models import (
    ActivityLog,
    Role,
    RoleAssignment,
    StudentProfile,
    User,
)
from apps.academic_structure.models import AcademicYear
from apps.system_settings.models import PasswordPolicy, SystemConfiguration

LOCK_PASSWORD = "OrigPass123!"


class RecoveryTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            email="admin@test.local", password="AdminPass123!"
        )

    def make_locked_user(self, email="student@test.local", password=LOCK_PASSWORD):
        user = User.objects.create_user(email=email, password=password)
        StudentProfile.objects.create(
            user=user,
            lrn="000000000042",
            first_name="Test",
            last_name="Student",
            birthdate=date(2009, 1, 15),
        )
        user.status = User.Status.LOCKED
        user.failed_login_attempts = 5
        user.locked_at = timezone.now()
        user.save()
        return user

    def make_staff_user(self, email="staff@test.local", role="registrar"):
        user = User.objects.create_user(email=email, password=LOCK_PASSWORD)
        user.status = User.Status.ACTIVE
        user.save()
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
        return user

    def make_signable_user(self, email="signin@test.local", password=LOCK_PASSWORD):
        """A locked student who can actually complete a login once unlocked.

        ``login_view`` refuses any account without a role assignment for the
        active academic year, so a recovery test that stops at the login
        response would otherwise be asserting on the role check rather than on
        the forced-password-change behaviour it means to cover.
        """
        user = self.make_locked_user(email=email, password=password)
        year = AcademicYear.objects.create(
            label="2026-2027",
            status=AcademicYear.STATUS_ACTIVE,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
        )
        role, _ = Role.objects.get_or_create(code=Role.Code.STUDENT)
        RoleAssignment.objects.create(user=user, role=role, academic_year=year)
        return user

    @staticmethod
    def grant(role_code, *codenames):
        """Give a role group the named ``accounts`` permissions."""
        group, _ = Group.objects.get_or_create(name=role_code)
        perms = Permission.objects.filter(
            content_type__app_label="accounts", codename__in=codenames
        )
        group.permissions.add(*perms)
        return perms


class UnlockUserAccountTests(RecoveryTestBase):
    def test_unlock_clears_status_counter_and_lock_timestamp(self):
        user = self.make_locked_user()

        cleared = unlock_user_account(user)

        user.refresh_from_db()
        self.assertEqual(cleared, 5)
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertEqual(user.failed_login_attempts, 0)
        self.assertIsNone(user.locked_at)
        # is_active is derived from status in User.save(); a still-inactive
        # account would pass every assertion above and still be refused login.
        self.assertTrue(user.is_active)

    def test_unlock_does_not_change_the_password(self):
        user = self.make_locked_user()

        unlock_user_account(user)

        user.refresh_from_db()
        self.assertTrue(authenticate(username=user.email, password=LOCK_PASSWORD))

    def test_unlock_records_the_administrator_who_did_it(self):
        user = self.make_locked_user()

        unlock_user_account(user, actor=self.admin)

        entry = ActivityLog.objects.filter(action="account_unlocked").latest("timestamp")
        self.assertEqual(entry.user, self.admin)
        self.assertEqual(entry.related_entity_id, user.pk)

    def test_unlocking_an_unlocked_account_is_refused(self):
        user = User.objects.create_user(email="ok@test.local", password=LOCK_PASSWORD)
        user.status = User.Status.ACTIVE
        user.save()

        with self.assertRaises(AccountActionError):
            unlock_user_account(user)


class LockoutThresholdTests(RecoveryTestBase):
    def test_repeated_failures_stamp_locked_at(self):
        user = User.objects.create_user(email="typo@test.local", password=LOCK_PASSWORD)
        user.status = User.Status.ACTIVE
        user.save()
        self.client.post(
            reverse("accounts:login"),
            data=json.dumps({"email": user.email, "password": "wrong"}),
            content_type="application/json",
        )

        user.refresh_from_db()
        self.assertEqual(user.failed_login_attempts, 1)
        self.assertIsNone(user.locked_at)

        for _ in range(4):
            self.client.post(
                reverse("accounts:login"),
                data=json.dumps({"email": user.email, "password": "wrong"}),
                content_type="application/json",
            )

        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.LOCKED)
        # Without this stamp the dashboard had to fall back to
        # password_changed_at and showed a lock date from whenever the password
        # last changed, which is usually months ago.
        self.assertIsNotNone(user.locked_at)

    def test_threshold_is_read_from_system_settings(self):
        SystemConfiguration.objects.update_or_create(pk=1, defaults={"account_lockout_threshold": 2})
        user = User.objects.create_user(email="quick@test.local", password=LOCK_PASSWORD)
        user.status = User.Status.ACTIVE
        user.save()

        for _ in range(2):
            self.client.post(
                reverse("accounts:login"),
                data=json.dumps({"email": user.email, "password": "wrong"}),
                content_type="application/json",
            )

        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.LOCKED)


class IssueTemporaryPasswordTests(RecoveryTestBase):
    def test_password_is_set_and_account_is_unlocked(self):
        user = self.make_locked_user()

        result = issue_temporary_password(user, "HandedOver123!", actor=self.admin)

        user.refresh_from_db()
        self.assertTrue(authenticate(username=user.email, password="HandedOver123!"))
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertEqual(user.failed_login_attempts, 0)
        self.assertIsNone(user.locked_at)
        self.assertTrue(result["was_locked"])

    def test_user_is_forced_to_choose_their_own_password(self):
        user = self.make_locked_user()

        issue_temporary_password(user, "HandedOver123!", actor=self.admin)

        user.refresh_from_db()
        self.assertTrue(user.must_change_password)

    def test_login_redirects_to_change_password_and_they_can_leave_it(self):
        user = self.make_signable_user()
        issue_temporary_password(user, "HandedOver123!")

        resp = self.client.post(
            reverse("accounts:login"),
            data=json.dumps({"email": user.email, "password": "HandedOver123!"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        redirect = json.loads(resp.content)["redirect_url"]
        self.assertIn(reverse("accounts:change_password"), redirect)

        # Any other page bounces back to the change-password screen, so the
        # admin-set password cannot simply be kept.
        dashboard = self.client.get(reverse("dashboards:student"))
        self.assertRedirects(
            dashboard,
            reverse("accounts:change_password") + "?reset=1",
            fetch_redirect_response=False,
        )

    def test_changing_the_password_clears_the_forced_flag(self):
        user = self.make_signable_user()
        issue_temporary_password(user, "HandedOver123!")
        self.client.post(
            reverse("accounts:login"),
            data=json.dumps({"email": user.email, "password": "HandedOver123!"}),
            content_type="application/json",
        )

        self.client.post(
            reverse("accounts:change_password"),
            {
                "current_password": "HandedOver123!",
                "new_password": "MyOwnPassword9!",
                "confirm_password": "MyOwnPassword9!",
            },
        )

        user.refresh_from_db()
        self.assertFalse(user.must_change_password)

    def test_generated_password_satisfies_the_policy(self):
        policy = PasswordPolicy.get_solo()
        for _ in range(25):
            password = generate_temporary_password()
            self.assertGreaterEqual(len(password), policy.minimum_length)
            if policy.require_lowercase:
                self.assertTrue(any(c.islower() for c in password))
            if policy.require_uppercase:
                self.assertTrue(any(c.isupper() for c in password))
            if policy.require_number:
                self.assertTrue(any(c.isdigit() for c in password))
            if policy.require_symbol:
                self.assertTrue(any(not c.isalnum() for c in password))

    def test_generated_passwords_are_not_repeated(self):
        generated = {generate_temporary_password() for _ in range(20)}
        self.assertEqual(len(generated), 20)

    def test_weak_password_is_refused_with_the_failing_rules(self):
        user = self.make_locked_user()

        with self.assertRaises(AccountActionError) as caught:
            issue_temporary_password(user, "abc")

        user.refresh_from_db()
        # The account must be untouched by a rejected attempt.
        self.assertEqual(user.status, User.Status.LOCKED)
        self.assertIn("at least", str(caught.exception))

    def test_archived_account_is_reported_rather_than_silently_reactivated(self):
        user = self.make_locked_user(email="gone@test.local")
        user.status = User.Status.ARCHIVED
        user.save()

        result = issue_temporary_password(user, "HandedOver123!")

        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.ARCHIVED)
        self.assertTrue(any("archived" in w for w in result["warnings"]))

    def test_activity_log_names_the_administrator(self):
        user = self.make_locked_user()

        issue_temporary_password(user, "HandedOver123!", actor=self.admin)

        entry = ActivityLog.objects.filter(action="admin_password_reset").latest("timestamp")
        self.assertEqual(entry.user, self.admin)


class SerializeLockedAccountTests(RecoveryTestBase):
    def test_payload_carries_the_id_and_a_real_reason(self):
        user = self.make_locked_user()

        row = serialize_locked_account(user, can_unlock=True, can_reset=True)

        self.assertEqual(row["id"], user.pk)
        self.assertEqual(row["email"], user.email)
        self.assertIn("5 failed sign-in attempt", row["reason"])
        self.assertNotIn("locked by administrator", row["reason"])
        self.assertTrue(row["canUnlock"])
        self.assertTrue(row["canReset"])
        self.assertTrue(row["isStudent"])

    def test_missing_lock_timestamp_is_stated_not_invented(self):
        user = self.make_locked_user()
        user.locked_at = None
        user.save()

        row = serialize_locked_account(user)

        self.assertEqual(row["when"], "Lock time not recorded")
        self.assertIsNone(row["locked_at"])


class AccountActionsApiTests(RecoveryTestBase):
    url = "/api/account-actions/"

    def setUp(self):
        self.client.force_login(self.admin)

    def post(self, payload):
        return self.client.post(
            self.url, data=json.dumps(payload), content_type="application/json"
        )

    def test_unlock_via_api_clears_the_lock(self):
        user = self.make_locked_user()

        resp = self.post({"action": "unlock", "user_id": user.pk})

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)["ok"])
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertEqual(user.failed_login_attempts, 0)

    def test_unlock_accepts_an_email_when_no_id_is_sent(self):
        user = self.make_locked_user()

        resp = self.post({"action": "unlock", "email": user.email})

        self.assertEqual(resp.status_code, 200)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.ACTIVE)

    def test_unknown_target_is_a_404_not_a_silent_success(self):
        resp = self.post({"action": "unlock", "user_id": 999999})
        self.assertEqual(resp.status_code, 404)

    def test_temp_password_returns_the_password_once(self):
        user = self.make_locked_user()

        resp = self.post({"action": "temp_password", "user_id": user.pk})

        body = json.loads(resp.content)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["password"])
        self.assertTrue(body["was_locked"])
        user.refresh_from_db()
        self.assertTrue(authenticate(username=user.email, password=body["password"]))

    def test_generate_password_needs_no_target(self):
        resp = self.post({"action": "generate_password"})

        body = json.loads(resp.content)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(body["password"])

    def test_get_returns_the_locked_list_for_the_card(self):
        user = self.make_locked_user()

        body = json.loads(self.client.get(self.url).content)

        self.assertTrue(body["ok"])
        self.assertEqual(body["locked_total"], 1)
        self.assertEqual(body["locked"][0]["id"], user.pk)

    def test_unknown_action_is_rejected(self):
        self.assertEqual(self.post({"action": "nonsense"}).status_code, 400)

    def test_get_is_not_a_state_changing_verb(self):
        self.assertEqual(self.client.put(self.url).status_code, 405)

    def test_anonymous_access_is_refused(self):
        self.client.logout()
        self.assertEqual(self.post({"action": "unlock", "user_id": 1}).status_code, 401)

    def test_administrator_cannot_recover_their_own_account(self):
        # An active administrator targeting themselves is refused: unlocking
        # would hand them a fresh set of attempts on a password they have just
        # proven they cannot type, and a temp password on their own account
        # would be a self-service bypass with an audit trail blaming the SA.
        self.assertEqual(self.admin.status, User.Status.ACTIVE)

        resp = self.post({"action": "unlock", "user_id": self.admin.pk})

        self.assertEqual(resp.status_code, 400)
        self.assertIn("your own account", json.loads(resp.content)["message"])
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.status, User.Status.ACTIVE)


class AccountActionsPermissionTests(RecoveryTestBase):
    """The registrar may unlock, but must not hand out passwords."""

    def setUp(self):
        self.user = self.make_locked_user()
        self.registrar = self.make_staff_user(role="registrar")
        granted = self.grant("registrar", "unlock_account")
        self.assertEqual(
            granted.count(),
            1,
            "accounts.unlock_account must exist and be grantable to the registrar",
        )
        self.client.force_login(self.registrar)

    def post(self, payload):
        return self.client.post(
            "/api/account-actions/", data=json.dumps(payload), content_type="application/json"
        )

    def test_unlock_is_allowed(self):
        resp = self.post({"action": "unlock", "user_id": self.user.pk})
        self.assertEqual(resp.status_code, 200)

    def test_temp_password_is_refused(self):
        resp = self.post({"action": "temp_password", "user_id": self.user.pk})
        self.assertEqual(resp.status_code, 403)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.LOCKED)

    def test_generate_password_is_refused(self):
        self.assertEqual(self.post({"action": "generate_password"}).status_code, 403)


class AccountRecoveryPageTests(RecoveryTestBase):
    def setUp(self):
        self.client.force_login(self.admin)

    def test_unlock_view_is_post_only(self):
        user = self.make_locked_user()

        resp = self.client.get(reverse("accounts:account_unlock", args=[user.pk]))

        self.assertEqual(resp.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.LOCKED)

    def test_unlock_view_unlocks_and_returns_to_the_student_list(self):
        user = self.make_locked_user()

        resp = self.client.post(reverse("accounts:account_unlock", args=[user.pk]))

        self.assertRedirects(resp, reverse("accounts:student_record_list"))
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.ACTIVE)

    def test_unlock_view_returns_an_employee_to_the_employee_list(self):
        staff = self.make_staff_user(email="emp@test.local", role="teacher")
        staff.status = User.Status.LOCKED
        staff.save()

        resp = self.client.post(reverse("accounts:account_unlock", args=[staff.pk]))

        self.assertRedirects(resp, reverse("accounts:employee_account_list"))

    def test_unlock_view_reports_a_double_unlock_instead_of_pretending(self):
        user = self.make_locked_user()

        self.client.post(reverse("accounts:account_unlock", args=[user.pk]))
        resp = self.client.post(
            reverse("accounts:account_unlock", args=[user.pk]), follow=True
        )

        self.assertContains(resp, "is not locked")

    def test_issue_password_page_prefills_a_generated_password(self):
        user = self.make_locked_user()

        resp = self.client.get(reverse("accounts:admin_reset_password", args=[user.pk]))

        self.assertEqual(resp.status_code, 200)
        prefilled = resp.context["form"]["new_password"].value()
        self.assertTrue(prefilled)
        self.assertGreaterEqual(len(prefilled), PasswordPolicy.get_solo().minimum_length)

    def test_issue_password_page_warns_that_a_locked_account_will_be_unlocked(self):
        user = self.make_locked_user()

        resp = self.client.get(reverse("accounts:admin_reset_password", args=[user.pk]))

        self.assertContains(resp, "locked")

    def test_generate_endpoint_returns_a_password_and_saves_nothing(self):
        before = User.objects.get(email=self.admin.email).password

        resp = self.client.get(reverse("accounts:admin_reset_password_generate"))

        self.assertTrue(json.loads(resp.content)["password"])
        self.assertEqual(User.objects.get(email=self.admin.email).password, before)

    def test_issuing_a_password_rejects_one_that_breaks_policy(self):
        user = self.make_locked_user()

        resp = self.client.post(
            reverse("accounts:admin_reset_password", args=[user.pk]),
            {"new_password": "abc"},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Password must include")
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.LOCKED)

    def test_registrar_without_permission_cannot_reach_the_page(self):
        registrar = self.make_staff_user(role="registrar")
        self.client.force_login(registrar)
        user = self.make_locked_user()

        resp = self.client.get(reverse("accounts:admin_reset_password", args=[user.pk]))

        self.assertEqual(resp.status_code, 403)


class StudentRecordStatusWriteTests(RecoveryTestBase):
    """Flipping the Account Status dropdown back to Active must clear the lock."""

    def test_reactivating_via_the_update_form_clears_the_lock(self):
        self.grant("registrar", "update_student_record")
        registrar = self.make_staff_user(role="registrar")
        self.client.force_login(registrar)
        user = self.make_locked_user()
        profile = user.student_profile

        resp = self.client.post(
            reverse("accounts:student_record_update", args=[profile.pk]),
            {
                "lrn": profile.lrn,
                "first_name": profile.first_name,
                "last_name": profile.last_name,
                "birthdate": profile.birthdate,
                "email": user.email,
                "status": User.Status.ACTIVE,
            },
        )

        self.assertIn(resp.status_code, (200, 302), getattr(resp, "context", None))
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.ACTIVE)
        self.assertEqual(user.failed_login_attempts, 0)
        self.assertIsNone(user.locked_at)
