"""Regression tests: verifying an email must actually let the user sign in.

`User.save()` recomputes ``is_active`` from ``status`` on every call. When a
caller passed ``update_fields=["status"]`` the recomputed flag was computed in
memory and then thrown away, so the row kept ``is_active=False``. Django's
default ``ModelBackend`` refuses to authenticate inactive users, so the account
verified successfully and *still* could not log in with the password that was
emailed to them.
"""

from django.contrib.auth import authenticate
from django.test import TestCase
from django.urls import reverse
import uuid

from django.utils import timezone

from .models import EmailVerificationToken, User


class VerifyEmailActivatesAccountTests(TestCase):
    def setUp(self):
        self.password = "TempPass123!"
        self.user = User.objects.create_user(
            email="verify@test.local", password=self.password
        )
        self.user.status = User.Status.PENDING_VERIFICATION
        self.user.save()
        self.user.refresh_from_db()

        self.token_value = uuid.uuid4()
        self.token = EmailVerificationToken.objects.create(
            user=self.user,
            token=self.token_value,
            expires_at=timezone.now() + timezone.timedelta(hours=48),
        )

    @property
    def verify_url(self):
        return reverse("accounts:verify_email", args=[self.token_value])

    def test_new_account_starts_inactive(self):
        self.assertEqual(self.user.status, User.Status.PENDING_VERIFICATION)
        self.assertFalse(self.user.is_active)

    def test_verify_email_sets_status_and_is_active(self):
        response = self.client.get(
            self.verify_url, follow=True
        )
        self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.ACTIVE)
        self.assertTrue(
            self.user.is_active,
            "verify_email must persist is_active, not just status",
        )

    def test_verified_user_can_authenticate_with_emailed_password(self):
        self.client.get(self.verify_url)
        self.user.refresh_from_db()

        authenticated = authenticate(
            username="verify@test.local", password=self.password
        )
        self.assertIsNotNone(
            authenticated,
            "a verified account must be able to sign in with its password",
        )

    def test_expired_token_leaves_account_pending(self):
        EmailVerificationToken.objects.filter(pk=self.token.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1)
        )
        self.client.get(self.verify_url)

        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.PENDING_VERIFICATION)
        self.assertFalse(self.user.is_active)
        self.assertIsNone(
            authenticate(username="verify@test.local", password=self.password)
        )


class StatusChangeKeepsIsActiveInSyncTests(TestCase):
    """Any partial save that moves `status` must carry `is_active` with it."""

    def _make(self, email):
        user = User.objects.create_user(email=email, password="TempPass123!")
        user.status = User.Status.PENDING_VERIFICATION
        user.save()
        return user

    def test_admin_activate_action_activates(self):
        user = self._make("admin-activate@test.local")
        user.status = User.Status.ACTIVE
        user.save(update_fields=["status", "is_active"])

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNotNone(
            authenticate(username=user.email, password="TempPass123!")
        )

    def test_archived_status_deactivates(self):
        user = self._make("archive@test.local")
        user.status = User.Status.ACTIVE
        user.save()
        user.refresh_from_db()
        self.assertTrue(user.is_active)

        user.status = User.Status.ARCHIVED
        user.save(update_fields=["status", "is_active"])
        user.refresh_from_db()
        self.assertFalse(user.is_active)