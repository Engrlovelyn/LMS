import json

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import ActivityLog, User


class PendingAccountDeleteTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="admin@example.test",
            password="Secure-Test-Password-123!",
        )
        self.client.force_login(self.admin)
        self.url = reverse("api_account_actions")

    def post_delete(self, user_id):
        return self.client.post(
            self.url,
            data=json.dumps({"action": "delete_pending", "user_id": user_id}),
            content_type="application/json",
        )

    def test_pending_account_can_be_removed_with_audit_record(self):
        pending = User.objects.create_user(
            email="pending@example.test",
            password="Secure-Test-Password-123!",
            status=User.Status.PENDING_VERIFICATION,
        )

        response = self.post_delete(pending.pk)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(User.objects.filter(pk=pending.pk).exists())
        self.assertTrue(
            ActivityLog.objects.filter(
                user=self.admin,
                action="pending_account_removed:pending@example.test",
                related_entity_id=pending.pk,
            ).exists()
        )

    def test_active_account_cannot_be_removed_by_pending_action(self):
        active = User.objects.create_user(
            email="active@example.test",
            password="Secure-Test-Password-123!",
            status=User.Status.ACTIVE,
        )

        response = self.post_delete(active.pk)

        self.assertEqual(response.status_code, 409)
        self.assertTrue(User.objects.filter(pk=active.pk).exists())
