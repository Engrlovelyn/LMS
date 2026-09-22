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
            "assessments:assessment_list",
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
            ("tass@tass.test", "tass_administrator", "dashboards:tass_administrator"),
            ("qa@tass.test", "quality_audit", "dashboards:quality_audit"),
        ]
        for email, role, url_name in cases:
            with self.subTest(role=role):
                self._login(email, role)
                resp = self.client.get(reverse(url_name))
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "Dashboard")

    def test_hub_requires_login_and_renders(self):
        redirect = self.client.get(reverse("dashboards:hub"))
        self.assertNotEqual(redirect.status_code, 200)
        self._login("hub@tass.test", "student")
        resp = self.client.get(reverse("dashboards:hub"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "San Vicente Institute")

    def test_wrong_role_blocked(self):
        user = self._login("teacher@tass.test", "teacher")
        resp = self.client.get(reverse("dashboards:ict"))
        self.assertEqual(resp.status_code, 403)


class NavLinkAccessTests(TestCase):
    """Every real page reachable from the dashboard sidebars must open for the
    role that links to it (no 403 / no redirect) after RBAC sync."""

    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)

    def _login(self, email, role):
        user = User.objects.create_user(email=email, password="pass12345x")
        user.status = "active"
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        self.client.login(username=email, password="pass12345x")

    def test_role_nav_pages_open(self):
        cases = {
            "student": [
                "classrooms:classroom_list",
                "communication:announcements",
                "communication:messages",
                "attendance:attendance_index",
                "assessments:performance_tracking",
                "accounts:change_password",
                "dashboards:hub",
            ],
            "teacher": [
                "classrooms:classroom_list",
                "teaching:repository_list",
                "assessments:assessment_repository",
                "assessments:assessment_list",
                "analytics:analytics_index",
                "accounts:change_password",
            ],
            "registrar": [
                "accounts:student_record_list",
                "academic:enrollment_list",
                "academic:section_list",
                "classrooms:classroom_list",
                "academic:validate_coverage",
                "academic:promote_students",
                "academic:academic_year_list",
                "academic:subject_list",
                "attendance:attendance_index",
                "analytics:analytics_index",
            ],
            "principal": [
                "accounts:employee_account_list",
                "classrooms:classroom_list",
                "teaching:repository_list",
                "assessments:assessment_list",
                "attendance:attendance_index",
                "assessments:performance_tracking",
                "analytics:analytics_index",
                "analytics:activity_logs",
                "communication:announcements",
            ],
            "assistant_principal": [
                "academic:academic_year_list",
                "classrooms:classroom_list",
                "teaching:repository_list",
                "assessments:assessment_list",
                "attendance:attendance_index",
                "assessments:performance_tracking",
                "analytics:analytics_index",
                "communication:announcements",
            ],
            "system_admin": [
                "accounts:employee_account_list",
                "accounts:role_assignment_list",
                "analytics:sf_report_home",
                "settings:password_policy",
                "analytics:activity_logs",
                "accounts:my_sessions",
                "settings:index",
                "settings:backups",
                "accounts:change_password",
            ],
            "quality_audit": [
                "assessments:performance_tracking",
                "classrooms:classroom_list",
                "attendance:attendance_report",
                "accounts:student_record_list",
                "analytics:activity_logs",
            ],
            "tass_administrator": [
                "accounts:employee_account_list",
                "governance:review_list",
                "analytics:activity_logs",
            ],
        }
        for role, url_names in cases.items():
            self._login("%s@tass.test" % role, role)
            for name in url_names:
                with self.subTest(role=role, url=name):
                    resp = self.client.get(reverse(name))
                    if name == "analytics:analytics_index":
                        # Landing hub that role-redirects to a sub-analytics page.
                        self.assertIn(resp.status_code, (200, 302))
                    else:
                        self.assertEqual(resp.status_code, 200)