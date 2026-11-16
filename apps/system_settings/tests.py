import json

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from .models import SystemConfiguration


class SystemSettingsApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="settings-admin@example.test",
            password="Secure-Test-Password-123!",
        )
        self.client.force_login(self.admin)
        self.url = reverse("api_system_settings")

    def test_saves_and_applies_session_and_lockout_settings(self):
        response = self.client.post(
            self.url,
            data=json.dumps({
                "sessionTimeout": 17,
                "lockoutThreshold": 4,
                "registration": "password_setup",
                "maintenance": True,
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        config = SystemConfiguration.get_solo()
        self.assertEqual(config.session_timeout_minutes, 17)
        self.assertEqual(config.account_lockout_threshold, 4)
        self.assertEqual(config.user_registration_policy, "password_setup")
        self.assertTrue(config.maintenance_mode)

    def test_rejects_invalid_timeout_instead_of_reporting_success(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"sessionTimeout": 0, "lockoutThreshold": 4}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
