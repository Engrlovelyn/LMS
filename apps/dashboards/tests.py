from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User


class RbacPermissionTests(TestCase):
    """View-level RBAC: bare codenames resolve, and sidebar-visible pages
    must not 403 for the roles that see them."""

    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)

    def _user(self, email, role):
        user = User.objects.create_user(email=email, password="pass12345x")
        user.status = "active"
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        self.client.login(username=email, password="pass12345x")
        return user

    def test_teacher_module_pages_accessible(self):
        self._user("teacher@tass.test", "teacher")
        for name in [
            "assessments:list",
            "attendance:attendance_index",
            "communication:announcements",
            "communication:messages",
        ]:
            with self.subTest(url=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_principal_messaging_blocked(self):
        self._user("principal@tass.test", "principal")
        self.assertEqual(self.client.get(reverse("communication:messages")).status_code, 403)
        self.assertEqual(self.client.get(reverse("attendance:attendance_index")).status_code, 200)

    def test_view_guarded_by_bare_codename(self):
        from apps.accounts.decorators import has_permission

        user = self._user("reg@tass.test", "registrar")
        self.assertTrue(has_permission(user, "view_student_record"))
        self.assertTrue(has_permission(user, "accounts.view_student_record"))
        self.assertFalse(has_permission(user, "nonexistent_perm"))


class DashboardRenderTests(TestCase):
    """Each role dashboard must render 200 with real (possibly empty) data."""

    def _login(self, email, role):
        user = User.objects.create_user(email=email, password="pass12345x")
        user.status = "active"
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        self.client.login(username=email, password="pass12345x")
        return user

    def test_dashboards_render(self):
        cases = [
            ("sa@tass.test", "system_admin", "dashboards:ict"),
            ("registrar@tass.test", "registrar", "dashboards:registrar"),
            ("teacher@tass.test", "teacher", "dashboards:teacher"),
            ("principal@tass.test", "principal", "dashboards:principal"),
            ("ap@tass.test", "assistant_principal", "dashboards:academic_coordinator"),
            ("student@tass.test", "student", "dashboards:student"),
        ]
        for email, role, url_name in cases:
            with self.subTest(role=role):
                self._login(email, role)
                resp = self.client.get(reverse(url_name))
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "Dashboard")

    def test_wrong_role_blocked(self):
        user = self._login("teacher@tass.test", "teacher")
        resp = self.client.get(reverse("dashboards:ict"))
        self.assertEqual(resp.status_code, 403)