from django.apps import apps as django_apps
from django.test import TestCase


class EnrollmentAppTests(TestCase):
    def test_app_registered(self):
        config = django_apps.get_app_config("enrollment")
        self.assertEqual(config.name, "apps.enrollment")
        self.assertEqual(config.label, "enrollment")

    def test_urls_load(self):
        from django.urls import reverse

        self.assertTrue(reverse("enrollment:dashboard"))