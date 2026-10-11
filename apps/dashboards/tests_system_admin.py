"""System Administrator dashboard regression tests.

These cover defects that made the ICT dashboard show dead data or crash:

* ``roleDirectory`` indexed ``a["user"]`` on dicts that never had a ``user``
  key, so every dashboard load raised ``KeyError``.
* the permission matrix compared qualified codenames
  (``attendance.view_attendance_records``) against the bare codenames stored on
  ``Permission`` (``view_attendance_records``), so no tick was ever checked.
* the Security Center tiles read ``securityStats.logins`` / ``.integrity`` /
  ``.alerts``, none of which the backend has ever emitted.
* role edits must reach ``RoleAssignment`` and the Django ``Group`` immediately
  rather than only sitting in localStorage.
"""

import html
import json
import re
from datetime import date
from pathlib import Path

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.accounts.models import EmployeeProfile, Role, RoleAssignment, User
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.dashboards.context import system_admin_context
from apps.virtual_classrooms.models import VirtualClassroom

PASSWORD = "pass12345x"


def make_user(email, status="active", first="Test", last="User"):
    user = User.objects.create_user(email=email, password=PASSWORD)
    user.status = status
    user.first_name = first
    user.last_name = last
    user.save()
    return user


class SystemAdminFixture(TestCase):
    """Runs the permission sync so Groups hold the grants the code defines."""

    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)
        for code, _label in Role.Code.choices:
            Role.objects.get_or_create(code=code)
        year = AcademicYear.objects.filter(status="ACTIVE").first()
        if not year:
            year = AcademicYear.objects.create(
                label="2099-2100",
                start_date="2099-01-01",
                end_date="2099-12-31",
                status="ACTIVE",
            )
        cls.year = year

    def login_admin(self, email="sa@tass.test"):
        admin = make_user(email, first="Sys", last="Admin")
        admin.groups.add(Group.objects.get(name="system_admin"))
        self.client.login(username=email, password=PASSWORD)
        return admin


class RoleDirectoryPayloadTests(SystemAdminFixture):
    def test_context_builds_without_keyerror(self):
        """Regression: the payload comprehension touched a missing key."""
        admin = make_user("sa@tass.test", first="Sys", last="Admin")
        ctx = system_admin_context(admin)  # must not raise
        self.assertIn("roleDirectory", ctx["dashboard_json"])

    def test_role_directory_marks_the_viewer_as_self(self):
        admin = self.login_admin()
        other = make_user("someone@tass.test", first="Some", last="One")
        ctx = system_admin_context(admin)
        by_email = {a["email"]: a for a in ctx["dashboard_json"]["roleDirectory"]}

        self.assertTrue(by_email[admin.email]["isSelf"])
        self.assertFalse(by_email[other.email]["isSelf"])
        # Every entry carries the fields the role manager renders.
        for entry in ctx["dashboard_json"]["roleDirectory"]:
            for key in ("id", "name", "initials", "gradient", "role_codes", "isSelf"):
                self.assertIn(key, entry)
            self.assertTrue(entry["initials"], "initials must not be empty")

    def test_dashboard_page_renders_for_admin(self):
        self.login_admin()
        resp = self.client.get(reverse("dashboards:ict"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "roleDirectory")


class PermissionMatrixTests(SystemAdminFixture):
    def test_matrix_reflects_real_group_permissions(self):
        """A granted permission must be checked; an ungranted one must not be."""
        matrix = system_admin_context(self.login_admin())["dashboard_json"]["roleMatrix"]
        rows = {r["codename"]: r["grants"] for r in matrix["rows"]}
        self.assertTrue(rows, "matrix must not be empty")

        teacher_group = Group.objects.get(name="teacher")
        granted = {
            "{}.{}".format(p.content_type.app_label, p.codename)
            for p in teacher_group.permissions.all()
        }
        self.assertTrue(granted, "teacher group should hold synced permissions")

        checked = 0
        for codename, grants in rows.items():
            if codename in granted:
                self.assertTrue(
                    grants["teacher"],
                    "teacher group holds %s but the matrix shows no grant" % codename,
                )
                checked += 1
            elif codename.split(".")[-1] not in {
                p.codename for p in teacher_group.permissions.all()
            }:
                self.assertFalse(
                    grants["teacher"],
                    "teacher group does not hold %s but the matrix claims it" % codename,
                )
        self.assertGreater(checked, 0, "no row matched a real teacher grant")

    def test_system_admin_group_holds_every_first_party_permission(self):
        group = Group.objects.get(name="system_admin")
        held = {p.codename for p in group.permissions.all()}
        expected = Permission.objects.filter(
            content_type__app_label__in=[
                "attendance",
                "assessments",
                "teaching_learning",
                "virtual_classrooms",
                "accounts",
                "academic_structure",
                "analytics",
                "dashboards",
                "enrollment",
                "governance",
                "system_settings",
            ]
        )
        self.assertTrue(expected.exists())
        missing = {p.codename for p in expected} - held
        self.assertEqual(missing, set(), "system_admin is missing: %s" % sorted(missing))


class SecurityCentrePayloadTests(SystemAdminFixture):
    def test_tile_keys_match_what_the_template_reads(self):
        """The tiles read logins24h / locked / unassigned."""
        ctx = system_admin_context(self.login_admin())
        stats = ctx["dashboard_json"]["securityStats"]
        for key in ("logins24h", "locked", "unassigned"):
            self.assertIn(key, stats)
            self.assertIsInstance(stats[key], int)
        # The old, never-emitted keys must not be relied upon any more.
        for dead in ("logins", "integrity", "alerts"):
            self.assertNotIn(dead, stats)

    def test_unassigned_counts_roleless_accounts(self):
        admin = self.login_admin()
        ctx = system_admin_context(admin)
        # The admin has a group; a roless active account must be counted.
        self.assertGreaterEqual(ctx["dashboard_json"]["securityStats"]["unassigned"], 1)

    def test_recent_login_rows_expose_frontend_field_names(self):
        """The modal renders n/e/a/t; the backend must send those keys."""
        from apps.accounts.models import ActivityLog
        from django.utils import timezone

        ActivityLog.objects.create(
            user=self.login_admin(),
            action="user_login",
            timestamp=timezone.now(),
        )
        ctx = system_admin_context(self.login_admin("sa2@tass.test"))
        rows = ctx["dashboard_json"]["recentLogins"]
        self.assertTrue(rows)
        for key in ("n", "e", "a", "t"):
            self.assertIn(key, rows[0])

    def test_alert_rows_expose_frontend_field_names(self):
        ctx = system_admin_context(self.login_admin())
        alerts = ctx["dashboard_json"]["alerts"]
        self.assertTrue(alerts, "expected at least the unassigned-role alert")
        for alert in alerts:
            for key in ("t", "d", "level"):
                self.assertIn(key, alert)


class UserRolesApiTests(SystemAdminFixture):
    def _post(self, payload, email="sa@tass.test"):
        return self.client.post(
            reverse("api_user_roles"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_requires_login(self):
        resp = self.client.post(
            reverse("api_user_roles"),
            data=json.dumps({"email": "x@tass.test", "roles": ["teacher"]}),
            content_type="application/json",
        )
        self.assertIn(resp.status_code, (302, 401, 403))

    def test_rejects_non_admin(self):
        teacher = make_user("t@tass.test")
        teacher.groups.add(Group.objects.get(name="teacher"))
        self.client.login(username="t@tass.test", password=PASSWORD)
        resp = self._post({"email": "someone@tass.test", "roles": ["registrar"]})
        self.assertEqual(resp.status_code, 403)

    def test_sets_multiple_roles_and_syncs_groups(self):
        self.login_admin()
        target = make_user("multi@tass.test", first="Multi", last="Role")

        resp = self._post({"user_id": target.pk, "roles": ["teacher", "registrar"]})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

        codes = set(
            target.role_assignments.filter(academic_year=self.year).values_list(
                "role__code", flat=True
            )
        )
        self.assertEqual(codes, {"teacher", "registrar"})

        # The Django Group membership is what page guards actually check.
        group_names = set(target.groups.values_list("name", flat=True))
        self.assertIn("teacher", group_names)
        self.assertIn("registrar", group_names)

    def test_replacing_roles_revokes_the_old_ones(self):
        self.login_admin()
        target = make_user("swap@tass.test")
        self._post({"user_id": target.pk, "roles": ["teacher", "registrar"]})
        self._post({"user_id": target.pk, "roles": ["teacher"]})

        codes = set(
            target.role_assignments.filter(academic_year=self.year).values_list(
                "role__code", flat=True
            )
        )
        self.assertEqual(codes, {"teacher"})
        self.assertNotIn("registrar", target.groups.values_list("name", flat=True))

    def test_rejects_empty_role_list(self):
        self.login_admin()
        target = make_user("none@tass.test")
        resp = self._post({"user_id": target.pk, "roles": []})
        self.assertEqual(resp.status_code, 400)

    def test_ignores_unknown_role_codes(self):
        self.login_admin()
        target = make_user("bogus@tass.test")
        resp = self._post({"user_id": target.pk, "roles": ["teacher", "wizard"]})
        self.assertEqual(resp.status_code, 200)
        codes = set(
            target.role_assignments.filter(academic_year=self.year).values_list(
                "role__code", flat=True
            )
        )
        self.assertEqual(codes, {"teacher"})

    def test_admin_cannot_revoke_own_system_admin_role(self):
        admin = self.login_admin()
        resp = self._post({"user_id": admin.pk, "roles": ["teacher"]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("system_admin", admin.groups.values_list("name", flat=True))

    def test_unknown_user_is_404(self):
        self.login_admin()
        resp = self._post({"email": "ghost@tass.test", "roles": ["teacher"]})
        self.assertEqual(resp.status_code, 404)

    def test_role_edit_does_not_archive_the_user(self):
        """The roster reconcile archives absent rows; a role edit must not."""
        self.login_admin()
        target = make_user("keep@tass.test")
        self._post({"user_id": target.pk, "roles": ["teacher"]})
        target.refresh_from_db()
        self.assertNotEqual(target.status, User.Status.ARCHIVED)

    def test_dashboard_shows_updated_roles_after_reload(self):
        self.login_admin()
        target = make_user("visible@tass.test", first="Vis", last="Ible")
        self._post({"user_id": target.pk, "roles": ["teacher", "registrar"]})

        resp = self.client.get(reverse("dashboards:ict"))
        self.assertEqual(resp.status_code, 200)
        by_email = {a["email"]: a for a in resp.context["dashboard_json"]["roleDirectory"]}
        self.assertEqual(
            sorted(by_email["visible@tass.test"]["role_codes"]),
            ["registrar", "teacher"],
        )


class AttendanceGroupedPayloadTests(SystemAdminFixture):
    """The attendance rows carry section/grade/tally fields the section-grouped
    view (view-attendance) uses to group classes under each section."""

    def test_attendance_rows_carry_section_and_tally_fields(self):
        teacher = make_user("tech@tass.test")
        EmployeeProfile.objects.create(
            user=teacher, employee_number="T-900", first_name="Mar", last_name="Garcia"
        )
        section = Section.objects.create(
            academic_year=self.year, name="Newton", grade_level=8
        )
        subject = Subject.objects.create(
            academic_year=self.year, name="Science", grade_level=8
        )
        vc = VirtualClassroom.objects.create(
            academic_year=self.year, section=section, subject=subject, teacher=teacher
        )
        session = AttendanceSession.objects.create(
            virtual_classroom=vc, session_date=date.today(), created_by=teacher
        )
        AttendanceRecord.objects.create(
            session=session, student=self._make_student("000000000999"),
            status=AttendanceRecord.PRESENT_ON_TIME,
        )
        AttendanceRecord.objects.create(
            session=session, student=self._make_student("000000001000"),
            status=AttendanceRecord.ABSENT_UNEXCUSED,
        )

        admin = make_user("sa@att.test", first="Sys", last="Admin")
        ctx = system_admin_context(admin)
        by_year = ctx["dashboard_json"]["attendance"]["byYear"]
        row = next(
            r for r in by_year[self.year.label]["rows"]
            if r["section"] == "Newton"
        )
        self.assertEqual(row["grade"], 8)
        self.assertEqual(row["subject"], "Science")
        self.assertEqual(row["id"], vc.id)
        self.assertEqual(row["present"], 1)
        self.assertEqual(row["total"], 2)

    def _make_student(self, lrn):
        from apps.accounts.models import StudentProfile

        user = User.objects.create_user(email=lrn + "@stu.test", password=PASSWORD)
        return StudentProfile.objects.create(
            user=user, lrn=lrn, first_name="Juan", last_name="Dela Cruz",
            birthdate=date(2010, 1, 1),
        )


class DashboardCalendarTests(SystemAdminFixture):
    """The activity calendar must survive the dashboard's own renderers.

    Regression: the security-alerts and online-users renderers addressed their
    cards *positionally* — ``#view-dashboard .card-body`` and
    ``#view-dashboard .card[1]``. Those only resolved correctly before the
    calendar card was added to the top of the dashboard, so once it existed the
    first match was the calendar's own card body: painting the alerts did
    ``innerHTML = ...`` over ``#calRoot``, the calendar never mounted, and the
    online list landed in the alerts card.
    """

    TEMPLATE = (
        Path(__file__).resolve().parents[2] / "templates" / "frontend" / "system_admin.html"
    )

    def setUp(self):
        self.source = self.TEMPLATE.read_text(encoding="utf-8")

    def test_dashboard_cards_are_addressed_by_id_not_by_position(self):
        """A positional selector will silently retarget the calendar card."""
        for bad in ("#view-dashboard .card-body", "#view-dashboard .card'[", '#view-dashboard .card"['):
            self.assertNotIn(
                bad,
                self.source,
                "%r addresses a dashboard card by position; use a stable id" % bad,
            )
        for good in ("#saAlertsCard", "#saOnlineCard"):
            self.assertIn(good, self.source, "%s must be selected by id" % good)

    def test_calendar_mount_target_and_script_are_present(self):
        self.login_admin()
        resp = self.client.get(reverse("dashboards:ict"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="calRoot"')
        self.assertContains(resp, "TASS.calendar.mount('calRoot'")
        self.assertContains(resp, "js/calendar.js")

    def test_calendar_payload_is_embedded_and_editable_for_admin(self):
        admin = self.login_admin("cal@tass.test")
        ctx = system_admin_context(admin)
        cal = ctx["dashboard_json"]["calendar"]
        self.assertTrue(cal["canEdit"], "a system administrator may add activities")
        self.assertIn("today", cal)
        self.assertIn("categories", cal)

        resp = self.client.get(reverse("dashboards:ict"))
        blob = re.search(
            r'<script id="system-admin-backend-data" type="application/json">(.*?)</script>',
            resp.content.decode(),
            re.S,
        )
        self.assertIsNotNone(blob, "the dashboard json_script blob is missing")
        payload = json.loads(html.unescape(blob.group(1)))
        self.assertEqual(payload["calendar"]["month"], cal["month"])
        self.assertEqual(payload["calendar"]["canEdit"], True)


class RolePermissionsApiTests(SystemAdminFixture):
    """POST /api/role-permissions/ — the editable permission matrix write path."""

    def _post(self, payload):
        return self.client.post(
            reverse("api_role_permissions"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_requires_login(self):
        resp = self._post(
            {"role": "teacher", "codename": "academic_structure.update_subject_custom", "grant": False}
        )
        self.assertIn(resp.status_code, (302, 401, 403))

    def test_rejects_non_admin(self):
        teacher = make_user("pm@tass.test")
        teacher.groups.add(Group.objects.get(name="teacher"))
        self.client.login(username="pm@tass.test", password=PASSWORD)
        resp = self._post(
            {"role": "teacher", "codename": "academic_structure.update_subject_custom", "grant": True}
        )
        self.assertEqual(resp.status_code, 403)

    def test_revokes_and_regrants_a_permission(self):
        self.login_admin()
        codename = "academic_structure.update_subject_custom"
        bare = codename.rsplit(".", 1)[-1]
        group = Group.objects.get(name="assistant_principal")
        self.assertTrue(group.permissions.filter(codename=bare).exists())

        resp = self._post({"role": "assistant_principal", "codename": codename, "grant": False})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertFalse(group.permissions.filter(codename=bare).exists())

        resp = self._post({"role": "assistant_principal", "codename": codename, "grant": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(group.permissions.filter(codename=bare).exists())

    def test_unknown_role_is_400(self):
        self.login_admin()
        resp = self._post(
            {"role": "wizard", "codename": "academic_structure.update_subject_custom", "grant": False}
        )
        self.assertEqual(resp.status_code, 400)

    def test_unknown_codename_is_400(self):
        self.login_admin()
        resp = self._post({"role": "teacher", "codename": "academic_structure.teleport", "grant": False})
        self.assertEqual(resp.status_code, 400)

    def test_system_admin_is_locked(self):
        self.login_admin()
        codename = "academic_structure.update_subject_custom"
        bare = codename.rsplit(".", 1)[-1]
        resp = self._post({"role": "system_admin", "codename": codename, "grant": False})
        self.assertEqual(resp.status_code, 400)
        group = Group.objects.get(name="system_admin")
        self.assertTrue(group.permissions.filter(codename=bare).exists())

    def test_sync_keeps_admin_edits(self):
        """assign_role_permissions must not resurrect an admin-revoked grant."""
        self.login_admin()
        codename = "communication.post_class_announcement"
        bare = codename.rsplit(".", 1)[-1]
        self._post({"role": "teacher", "codename": codename, "grant": False})
        self.assertFalse(Group.objects.get(name="teacher").permissions.filter(codename=bare).exists())

        call_command("assign_role_permissions", verbosity=0)
        self.assertFalse(Group.objects.get(name="teacher").permissions.filter(codename=bare).exists())


class SectionAssignmentTests(SystemAdminFixture):
    """Sections must be fillable from the dashboard, not just readable.

    Regression: the Sections table had no Actions column, so a section created on
    this dashboard stayed empty until the registrar sectioned its learners by
    hand, and there was no way to import a whole grade level from a CSV.
    """

    TEMPLATE = (
        Path(__file__).resolve().parents[2] / "templates" / "frontend" / "system_admin.html"
    )

    def setUp(self):
        self.source = self.TEMPLATE.read_text(encoding="utf-8")

    def test_sections_table_offers_both_assignment_paths(self):
        self.login_admin()
        resp = self.client.get(reverse("dashboards:ict"))
        self.assertEqual(resp.status_code, 200)
        # The actions live in the row renderer, so both paths must be there.
        for control in ("openSectionStudents(", "openSectionBulk()"):
            self.assertIn(control, self.source, "%s must be reachable from a row" % control)
        self.assertIn('id="sectionStudentsModal"', self.source)
        self.assertIn('id="sectionBulkModal"', self.source)
        self.assertIn('id="secAsgRows"', self.source)
        self.assertIn('id="secBulkText"', self.source)

    def test_add_section_offers_the_roster_picker(self):
        """Creating a section hands over to the picker instead of stopping."""
        self.login_admin()
        resp = self.client.get(reverse("dashboards:ict"))
        self.assertContains(resp, 'id="saSecAssign"')
        self.assertIn("openSectionStudents(row.id)", self.source)

    def test_assignment_posts_to_the_student_enrollment_endpoint(self):
        """Both paths reuse /api/students/ rather than a second write path."""
        body = self.source.split("function submitSectionStudents", 1)[1].split("function openSectionBulk", 1)[0]
        self.assertIn("saPostJson('/api/students/'", body)
        bulk = self.source.split("function submitSectionBulk", 1)[1].split(
            "function downloadSectionBulkTemplate", 1
        )[0]
        self.assertIn("saPostJson('/api/students/'", bulk)

    def test_bulk_import_never_invents_a_learner(self):
        """A CSV LRN with no roster match is skipped, not created."""
        self.assertIn("matchSectionBulk", self.source)
        parser = self.source.split("function parseSectionBulkLrns", 1)[1].split(
            "function matchSectionBulk", 1
        )[0]
        # No direct write: the rows are resolved through the roster first.
        self.assertNotIn("saPostJson", parser)


class AccountRecoveryControlsTests(SystemAdminFixture):
    """Unlock and password recovery must be reachable from the Accounts board.

    Regression: the edit-modal status dropdown had no "Locked" option, so opening
    a locked employee fell back to showing "Active" and saving the form unlocked
    the account through the roster write -- no unlock action, no audit entry, no
    permission check. Recovery now has to be a deliberate click.
    """

    TEMPLATE = (
        Path(__file__).resolve().parents[2] / "templates" / "frontend" / "system_admin.html"
    )

    def setUp(self):
        self.source = self.TEMPLATE.read_text(encoding="utf-8")

    def test_edit_modal_offers_locked_status_and_unlock(self):
        self.login_admin()
        resp = self.client.get(reverse("dashboards:ict"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="editUnlockBtn"')
        self.assertContains(resp, 'id="editLockedNote"')
        modal = self.source.split('id="editStatus"', 1)[1].split("</select>", 1)[0]
        self.assertIn(
            "<option>Locked</option>",
            modal,
            "a locked account must not fall back to Active in the status dropdown",
        )

    def test_accounts_rows_offer_unlock_only_while_locked(self):
        self.assertIn("u.status==='Locked'", self.source)
        self.assertIn("confirmUnlockAccount('+u.id+", self.source)

    def test_password_recovery_uses_the_guarded_endpoint(self):
        """Both recovery actions must go through /api/account-actions/."""
        for fn, action in (
            ("function confirmUnlockAccount", "{action:'unlock'"),
            ("function submitTempPassword", "{action:'temp_password'"),
        ):
            body = self.source.split(fn, 1)[1][:2000]
            self.assertIn("accountActionUrl()", body, "%s must post to the guarded endpoint" % fn)
            self.assertIn(action, body)

    def test_unlock_refreshes_the_roster_row(self):
        """The table must not keep showing Locked for someone who can sign in."""
        self.assertIn("function saMarkAccountUnlocked", self.source)
        unlock = self.source.split("function confirmUnlockAccount", 1)[1].split(
            "function openTempPasswordModal", 1
        )[0]
        self.assertIn("saMarkAccountUnlocked(userId)", unlock)


class DemographicsTests(SystemAdminFixture):
    """Staff and student Age/Sex panels plus the SF template upload flow.

    These pin the fixes for the three reported gaps:
    * the admin dashboard had no learner demographics panel;
    * staff demographics had no sex/birthdate source to read or edit;
    * SF2 templates had no upload entry point for the administrator.
    """

    TEMPLATE = (
        Path(__file__).resolve().parents[2] / "templates" / "frontend" / "system_admin.html"
    )

    def setUp(self):
        self.source = self.TEMPLATE.read_text(encoding="utf-8")

    def test_learner_panel_is_rendered(self):
        self.login_admin()
        resp = self.client.get(reverse("dashboards:ict"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="view-demographics"')  # staff panel kept
        self.assertContains(resp, 'id="view-studentdemographics"')
        self.assertContains(resp, "Student Demographics")
        self.assertContains(resp, "showView('studentdemographics')")
        self.assertIn("'studentdemographics':1", self.source)  # router permit
        self.assertIn("window.__saStaffDemo", self.source)
        self.assertIn("window.__saStudentDemo", self.source)

    def test_payload_carries_both_demographics(self):
        admin = self.login_admin()
        staff = make_user("staff@tass.test", first="Staf", last="One")
        EmployeeProfile.objects.create(
            user=staff,
            employee_number="AMP-001",
            first_name="Staf",
            last_name="One",
            sex="F",
            birthdate=date(1992, 6, 15),
        )
        ctx = system_admin_context(admin)
        payload = ctx["dashboard_json"]
        self.assertIn("demographics", payload)
        self.assertIn("studentDemographics", payload)
        staff_row = next(
            r for r in payload["demographics"]["rows"] if r["name"].startswith("Staf")
        )
        self.assertEqual(staff_row["gender"], "Female")
        self.assertEqual(staff_row["birth"], "1992-06-15")
        account = next(a for a in payload["accounts"] if a["email"] == "staff@tass.test")
        self.assertIn("sex", account)
        self.assertIn("birthdate", account)

    def test_edit_modal_records_sex_and_birthdate(self):
        self.assertIn('id="editSex"', self.source)
        self.assertIn('id="editBirthdate"', self.source)
        modal = self.source.split("function confirmUpdateEmployee", 1)[1][:4000]
        self.assertIn("'/api/accounts/demographics/'", modal)
        self.assertIn("editBirthdate", modal)

    def test_staff_panel_hints_when_values_are_missing(self):
        self.assertIn('id="dmNote"', self.source)

    def test_upload_template_flow_present(self):
        """Admins can push a new SF2 template that teachers pick up."""
        self.assertIn('id="templateUploadModal"', self.source)
        self.assertIn("openTemplateUploadModal()", self.source)
        upload = self.source.split("function submitTemplateUpload", 1)[1][:3000]
        self.assertIn("'/analytics/sf/templates/upload/'", upload)
        self.assertIn("FormData", upload)
        self.assertIn("'template_file'", upload)
