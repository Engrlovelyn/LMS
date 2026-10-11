"""Profile self-service endpoint (``POST /api/profile/``).

The dashboard "My Profile" modal posts here so a learner (or staff member) can
persist their own name, contact number, email and photo. These tests drive the
real endpoint rather than the model, because the front end depends on the exact
response shape (``ok`` + ``avatar``) and on duplicate-email handling.
"""

import datetime
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import StudentProfile, User

PROFILE_URL = "/api/profile/"


def _tiny_gif():
    # 1x1 transparent GIF.
    return (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
        b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
    )


class StudentProfileEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="learner@test.local", password="ProfilePass123!"
        )
        cls.user.status = User.Status.ACTIVE
        cls.user.save()
        cls.profile = StudentProfile.objects.create(
            user=cls.user,
            lrn="LRN-LEARNER",
            first_name="Juan",
            last_name="Cruz",
            sex="M",
            birthdate=datetime.date(2013, 5, 4),
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_updates_name_contact_and_email(self):
        response = self.client.post(
            PROFILE_URL,
            {
                "name": "Maria Santos",
                "email": "maria.santos@test.local",
                "contact": "09171234567",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

        self.profile.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(self.profile.first_name, "Maria")
        self.assertEqual(self.profile.last_name, "Santos")
        self.assertEqual(self.profile.contact_number, "09171234567")
        self.assertEqual(self.user.email, "maria.santos@test.local")

    def test_duplicate_email_is_not_taken(self):
        User.objects.create_user(email="taken@test.local", password="OtherPass123!")
        response = self.client.post(
            PROFILE_URL,
            {"name": "Juan Cruz", "email": "taken@test.local", "contact": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "learner@test.local")

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(PROFILE_URL).status_code, 405)

    def test_anonymous_cannot_post(self):
        self.assertEqual(Client().post(PROFILE_URL, {"name": "X"}).status_code, 302)


class StudentProfileAvatarTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()

        self.user = User.objects.create_user(
            email="pictured@test.local", password="ProfilePass123!"
        )
        self.user.status = User.Status.ACTIVE
        self.user.save()
        self.profile = StudentProfile.objects.create(
            user=self.user,
            lrn="LRN-PIC",
            first_name="Pia",
            last_name="Reyes",
            sex="F",
            birthdate=datetime.date(2013, 2, 2),
        )
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_avatar_upload_is_stored_and_returned(self):
        upload = SimpleUploadedFile(
            "me.gif", _tiny_gif(), content_type="image/gif"
        )
        response = self.client.post(
            PROFILE_URL,
            {"name": "Pia Reyes", "email": "pictured@test.local", "avatar": upload},
        )
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.avatar)
        self.assertTrue(response.json().get("avatar"))
