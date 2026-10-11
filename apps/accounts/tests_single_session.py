"""One active device per account (non-functional requirement).

Drives the real ``/accounts/api/login/`` endpoint with separate test clients,
which is the closest a unit test can get to two physical devices: each client
carries its own session cookie and therefore its own server-side session row.
"""

import json
from datetime import date, timedelta

from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.academic_structure.models import AcademicYear
from apps.accounts.models import LoginSession, Role, RoleAssignment, User
from apps.accounts import session_guard

PASSWORD = "SessionPass123!"


class SingleSessionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            status=AcademicYear.STATUS_ACTIVE,
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
        )

    def make_user_with_role(self, email, role_code):
        user = User.objects.create_user(email=email, password=PASSWORD)
        user.status = User.Status.ACTIVE
        user.save()
        role, _ = Role.objects.get_or_create(code=role_code)
        RoleAssignment.objects.create(user=user, role=role, academic_year=self.year)
        group, _ = Group.objects.get_or_create(name=role_code)
        user.groups.add(group)
        return user

    def login(self, client, email, password=PASSWORD):
        return client.post(
            reverse("accounts:login"),
            data=json.dumps({"email": email, "password": password}),
            content_type="application/json",
        )

    # -- core policy ---------------------------------------------------
    def test_first_login_succeeds(self):
        user = self.make_user_with_role("teacher1@test.local", "teacher")
        client = Client()
        response = self.login(client, user.email)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(LoginSession.objects.filter(user=user).count(), 1)

    def test_second_device_is_rejected(self):
        user = self.make_user_with_role("teacher2@test.local", "teacher")
        device_a = Client()
        self.assertEqual(self.login(device_a, user.email).status_code, 200)

        device_b = Client()
        response = self.login(device_b, user.email)
        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "active_session")
        self.assertEqual(LoginSession.objects.filter(user=user).count(), 1)

    def test_login_after_logout_succeeds(self):
        user = self.make_user_with_role("teacher3@test.local", "teacher")
        device_a = Client()
        self.login(device_a, user.email)

        device_a.get(reverse("accounts:logout"))
        self.assertEqual(LoginSession.objects.filter(user=user).count(), 0)

        device_b = Client()
        self.assertEqual(self.login(device_b, user.email).status_code, 200)

    def test_expired_session_is_recycled(self):
        user = self.make_user_with_role("teacher4@test.local", "teacher")
        device_a = Client()
        self.login(device_a, user.email)

        # Simulate the browser session aging out server-side without a logout.
        session = LoginSession.objects.get(user=user)
        Session.objects.filter(session_key=session.session_key).update(
            expire_date=timezone.now() - timedelta(hours=1)
        )

        device_b = Client()
        self.assertEqual(self.login(device_b, user.email).status_code, 200)

    def test_abandoned_session_is_recycled(self):
        user = self.make_user_with_role("teacher5@test.local", "teacher")
        device_a = Client()
        self.login(device_a, user.email)

        # The device row looks live by session, but has been idle past the limit.
        LoginSession.objects.filter(user=user).update(
            last_active_at=timezone.now() - timedelta(hours=5)
        )

        device_b = Client()
        self.assertEqual(self.login(device_b, user.email).status_code, 200)

    def test_different_users_login_independently(self):
        user1 = self.make_user_with_role("t6a@test.local", "teacher")
        user2 = self.make_user_with_role("t6b@test.local", "teacher")
        self.assertEqual(self.login(Client(), user1.email).status_code, 200)
        self.assertEqual(self.login(Client(), user2.email).status_code, 200)

    def test_each_role_can_hold_a_session(self):
        for role_code in (
            "system_admin",
            "registrar",
            "principal",
            "elementary_principal",
            "assistant_principal",
            "teacher",
            "student",
            "quality_audit",
        ):
            with self.subTest(role=role_code):
                user = self.make_user_with_role(
                    "{}@test.local".format(role_code), role_code
                )
                response = self.login(Client(), user.email)
                self.assertEqual(response.status_code, 200)

    def test_same_browser_relogin_is_allowed(self):
        user = self.make_user_with_role("teacher7@test.local", "teacher")
        client = Client()
        self.assertEqual(self.login(client, user.email).status_code, 200)
        # Same client/cookies: re-authenticating must not lock the user out.
        second = self.login(client, user.email)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(LoginSession.objects.filter(user=user).count(), 1)

    # -- helper-level behaviour ---------------------------------------
    def test_purge_stale_sessions_ignores_client_supplied_key(self):
        user = self.make_user_with_role("teacher8@test.local", "teacher")
        LoginSession.objects.create(
            user=user, session_key="forged-key-that-does-not-exist", is_current=True
        )
        # A non-existent Django session means the row is not live.
        self.assertIsNone(session_guard.active_session_for(user))
        self.assertEqual(LoginSession.objects.filter(user=user).count(), 0)

    def test_release_all_sessions_clears_every_device(self):
        user = self.make_user_with_role("teacher9@test.local", "teacher")
        self.login(Client(), user.email)
        self.assertEqual(LoginSession.objects.filter(user=user).count(), 1)
        session_guard.release_all_sessions(user)
        self.assertEqual(LoginSession.objects.filter(user=user).count(), 0)
