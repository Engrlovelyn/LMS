import json

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
            ("elem-principal@tass.test", "elementary_principal", "dashboards:principal"),
            ("ap@tass.test", "assistant_principal", "dashboards:academic_coordinator"),
            ("student@tass.test", "student", "dashboards:student"),
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


class PrincipalQAEnrollmentTests(TestCase):
    """Quality Assurance and the Principal share one screen (principal.html),
    so both must expose the same enrollment / demographics payload."""

    @classmethod
    def setUpTestData(cls):
        import datetime
        from apps.accounts.models import StudentProfile
        from apps.academic_structure.models import (
            AcademicYear, AcademicStructureVersion, Section, StudentEnrollment,
        )
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2027, 5, 31),
            status="ACTIVE",
        )
        cls.version = AcademicStructureVersion.objects.create(
            academic_year=cls.year, grading_period_type="QUARTER"
        )
        cls.section = Section.objects.create(
            academic_year=cls.year, name="Rose", grade_level=7
        )
        user = User.objects.create_user(
            email="learner@tass.test", password="pass12345x"
        )
        user.status = "active"
        user.save()
        prof = StudentProfile.objects.create(
            user=user, lrn="LRN-100", first_name="Ana", last_name="Top",
            sex="F", birthdate=datetime.date(2013, 5, 4),
        )
        StudentEnrollment.objects.create(
            student=prof, academic_year=cls.year, section=cls.section
        )

    def _login(self, role):
        u = User.objects.create_user(
            email="%s@tass.test" % role, password="pass12345x"
        )
        u.status = "active"
        u.save()
        u.groups.add(Group.objects.get_or_create(name=role)[0])
        self.client.login(username="%s@tass.test" % role, password="pass12345x")

    def test_both_roles_share_enrollment_payload(self):
        for email, role, url_name in [
            ("principal@tass.test", "principal", "dashboards:principal"),
            ("qa@tass.test", "quality_audit", "dashboards:quality_audit"),
        ]:
            with self.subTest(role=role):
                self._login(role)
                resp = self.client.get(reverse(url_name))
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "Enrollment Status")
                self.assertContains(resp, "Student Demographics")
                self.assertContains(resp, "enrollStudents")
                self.assertContains(resp, "enrollSections")
                self.assertContains(resp, "Grade 7")
                self.assertContains(resp, "LRN-100")
                self.client.logout()


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

class DemographicsPanelTests(TestCase):
    """Age/Sex panels: learners on the staff dashboards, employees on ICT.

    Every row uses the same shape, so one front-end renderer draws either list
    and an empty ``gender``/``birth`` reaches the browser as an empty string
    (rendered as an em dash) instead of a guessed value.
    """

    def _login(self, email, role):
        user = User.objects.create_user(email=email, password="pass12345x")
        user.status = "active"
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        self.client.login(username=email, password="pass12345x")
        return user

    def test_each_dashboard_ships_demographics_rows(self):
        cases = [
            ("sa@tass.test", "system_admin", "dashboards:ict", "demographics"),
            ("registrar@tass.test", "registrar", "dashboards:registrar", "demographics"),
            ("teacher@tass.test", "teacher", "dashboards:teacher", "demographics"),
            ("ap@tass.test", "assistant_principal", "dashboards:academic_coordinator", "demographics"),
            ("principal@tass.test", "principal", "dashboards:principal", "staffDemographics"),
            ("qa@tass.test", "quality_audit", "dashboards:quality_audit", "staffDemographics"),
        ]
        for email, role, url_name, key in cases:
            with self.subTest(role=role):
                self._login(email, role)
                resp = self.client.get(reverse(url_name))
                self.assertEqual(resp.status_code, 200)
                payload = resp.context["dashboard_json"]
                self.assertIn(key, payload)
                self.assertIn("rows", payload[key])
                self.assertIsInstance(payload[key]["rows"], list)
                self.assertContains(resp, 'id="view-demographics"')
                self.assertContains(resp, "TASS.demo")
                self.client.logout()

    def test_employee_sex_and_birthdate_reach_the_ict_panel(self):
        import datetime

        from apps.accounts.models import EmployeeProfile

        self._login("sa2@tass.test", "system_admin")
        staff = User.objects.create_user(
            email="staff@tass.test", password="pass12345x"
        )
        staff.status = "active"
        staff.save()
        EmployeeProfile.objects.create(
            user=staff,
            employee_number="EMP-001",
            first_name="Juana",
            last_name="Dela Cruz",
            sex="F",
            birthdate=datetime.date(1990, 5, 4),
        )
        unrecorded = User.objects.create_user(
            email="staff2@tass.test", password="pass12345x"
        )
        unrecorded.status = "active"
        unrecorded.save()
        EmployeeProfile.objects.create(
            user=unrecorded,
            employee_number="EMP-002",
            first_name="Pepe",
            last_name="Santos",
        )

        payload = self.client.get(reverse("dashboards:ict")).context[
            "dashboard_json"
        ]
        rows = {r["code"]: r for r in payload["demographics"]["rows"]}
        self.assertIn("EMP-001", rows)
        self.assertEqual(rows["EMP-001"]["gender"], "Female")
        self.assertEqual(rows["EMP-001"]["birth"], "1990-05-04")
        # No sex/birthdate on file: empty strings, never a guessed default.
        self.assertIn("EMP-002", rows)
        self.assertEqual(rows["EMP-002"]["gender"], "")
        self.assertEqual(rows["EMP-002"]["birth"], "")

def test_shared_renderer_is_loaded(self):
        for url_name, role in (
            ("dashboards:ict", "system_admin"),
            ("dashboards:registrar", "registrar"),
            ("dashboards:teacher", "teacher"),
        ):
            with self.subTest(url=url_name):
                self._login(url_name + "@tass.test", role)
                resp = self.client.get(reverse(url_name))
                self.assertContains(resp, "js/demographics.js")
                self.client.logout()


class PrincipalCampusScopeTests(TestCase):
    """Both principals share one screen but must never see the other campus.

    The Elementary Principal (Grades 1-6) and the High School Principal
    (Grades 7-12) reach the same ``dashboards:principal`` view. The scope is
    derived from the role codes in their RoleAssignments -- the same rows the
    group sync reads -- so the dashboard rows and announcement feed have to
    come from their campus only.
    """

    @classmethod
    def setUpTestData(cls):
        import datetime

        from apps.accounts.models import Role
        from apps.academic_structure.models import (
            AcademicYear, Section, Subject,
        )
        from apps.communication.models import SchoolAnnouncement
        from apps.virtual_classrooms.models import VirtualClassroom

        cls.teacher = User.objects.create_user(
            email="faculty@tass.test", password="pass12345x"
        )
        cls.teacher.status = "active"
        cls.teacher.save()
        cls.role_ep = Role.objects.get_or_create(code="elementary_principal")[0]
        cls.role_hs = Role.objects.get_or_create(code="principal")[0]
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2027, 5, 31),
            status="ACTIVE",
        )
        cls.section_elem = Section.objects.create(
            academic_year=cls.year, name="Malvar", grade_level=3
        )
        cls.section_hs = Section.objects.create(
            academic_year=cls.year, name="Rizal", grade_level=9
        )
        subj_elem = Subject.objects.create(
            academic_year=cls.year, name="Science", grade_level=3
        )
        subj_hs = Subject.objects.create(
            academic_year=cls.year, name="Physics", grade_level=9
        )
        cls.vc_elem = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section_elem,
            subject=subj_elem,
            teacher=cls.teacher,
        )
        cls.vc_hs = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section_hs,
            subject=subj_hs,
            teacher=cls.teacher,
        )
        SchoolAnnouncement.objects.create(
            title="Elementary Foundation Day",
            content="Parade and booths for the lower grades.",
            posted_by=cls.teacher,
            audience="elementary",
        )
        SchoolAnnouncement.objects.create(
            title="High School Prom",
            content="Junior and Senior High only.",
            posted_by=cls.teacher,
            audience="high_school",
        )

    def _login(self, email, role_code, role_obj):
        from apps.accounts.models import RoleAssignment

        user = User.objects.create_user(email=email, password="pass12345x")
        user.status = "active"
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role_code)[0])
        RoleAssignment.objects.create(
            user=user, role=role_obj, academic_year=self.year
        )
        self.client.login(username=email, password="pass12345x")
        return user

    def test_elementary_principal_sees_only_grades_1_6(self):
        self._login("elem-principal@tass.test", "elementary_principal", self.role_ep)
        payload = self.client.get(reverse("dashboards:principal")).context["dashboard_json"]
        self.assertEqual(payload["campus"]["code"], "elementary")
        self.assertEqual(payload["campus"]["scopes"], ["elementary"])
        class_titles = [c["title"] for c in payload["classes"]]
        self.assertTrue(any("Grade 3" in t for t in class_titles), class_titles)
        self.assertFalse(any("Grade 9" in t or "Physics" in t for t in class_titles))
        ann_titles = [a["title"] for a in payload["announcements"]]
        self.assertIn("Elementary Foundation Day", ann_titles)
        self.assertNotIn("High School Prom", ann_titles)

    def test_hs_principal_sees_only_grades_7_12(self):
        self._login("hs-principal@tass.test", "principal", self.role_hs)
        payload = self.client.get(reverse("dashboards:principal")).context["dashboard_json"]
        self.assertEqual(payload["campus"]["code"], "high_school")
        self.assertEqual(payload["campus"]["scopes"], ["high_school"])
        class_titles = [c["title"] for c in payload["classes"]]
        self.assertTrue(any("Grade 9" in t for t in class_titles), class_titles)
        self.assertFalse(any("Grade 3" in t or "Science" in t for t in class_titles))
        ann_titles = [a["title"] for a in payload["announcements"]]
        self.assertIn("High School Prom", ann_titles)
        self.assertNotIn("Elementary Foundation Day", ann_titles)


class RegistrarMonitoringPayloadTests(TestCase):
    """Attendance Overview, SF2, Attendance Summary and the At-Risk Students
    report all read one registrar payload; the Staff Demographics panel reads
    another. Both used to be shipped empty, so every one of those screens
    rendered "No data yet" no matter what was recorded."""

    @classmethod
    def setUpTestData(cls):
        import datetime

        from apps.accounts.models import EmployeeProfile, StudentProfile
        from apps.academic_structure.models import (
            AcademicYear, Section, Subject,
        )
        from apps.attendance.models import AttendanceRecord, AttendanceSession
        from apps.virtual_classrooms.models import VirtualClassroom

        cls.teacher = User.objects.create_user(
            email="faculty@tass.test", password="pass12345x"
        )
        cls.teacher.status = "active"
        cls.teacher.save()
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2027, 5, 31),
            status="ACTIVE",
        )
        section = Section.objects.create(
            academic_year=cls.year, name="Rizal", grade_level=9
        )
        subject = Subject.objects.create(
            academic_year=cls.year, name="Science", grade_level=9
        )
        vc = VirtualClassroom.objects.create(
            academic_year=cls.year, section=section, subject=subject,
            teacher=cls.teacher,
        )
        cls.student_user = User.objects.create_user(
            email="136100100001@stu.test", password="pass12345x"
        )
        cls.student = StudentProfile.objects.create(
            user=cls.student_user,
            lrn="136100100001",
            first_name="Maria",
            last_name="Santos",
            sex="F",
            birthdate=datetime.date(2012, 5, 14),
        )
        # One record per session: (session, student) is unique, so each status
        # gets its own school day — all inside June, the month the client's
        # month filter groups on.
        for day, status, remark in (
            (15, AttendanceRecord.PRESENT_ON_TIME, ""),
            (16, AttendanceRecord.PRESENT_LATE, ""),
            (17, AttendanceRecord.ABSENT_UNEXCUSED, ""),
            (18, AttendanceRecord.ABSENT_EXCUSED, "Medical leave with a note."),
        ):
            session = AttendanceSession.objects.create(
                virtual_classroom=vc,
                session_date=datetime.date(2026, 6, day),
                created_by=cls.teacher,
            )
            AttendanceRecord.objects.create(
                session=session, student=cls.student, status=status,
                remark=remark,
            )

        cls.staff_user = User.objects.create_user(
            email="staff@tass.test", password="pass12345x"
        )
        cls.staff_user.status = "active"
        cls.staff_user.save()
        EmployeeProfile.objects.create(
            user=cls.staff_user,
            employee_number="EMP-101",
            first_name="Juana",
            last_name="Dela Cruz",
            sex="F",
            birthdate=datetime.date(1990, 5, 4),
        )

    def _login(self, email, role):
        user = User.objects.create_user(email=email, password="pass12345x")
        user.status = "active"
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        self.client.login(username=email, password="pass12345x")
        return user

    def test_attendance_rows_are_grouped_per_learner_subject_month(self):
        self._login("reg-att@tass.test", "registrar")
        payload = self.client.get(reverse("dashboards:registrar")).context[
            "dashboard_json"
        ]
        rows = payload["attendance"]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["lrn"], "136100100001")
        self.assertEqual(row["subject"], "Science")
        self.assertEqual(row["month"], "June")
        self.assertEqual(
            {k: row[k] for k in ("present", "late", "absent", "excused")},
            {"present": 1, "late": 1, "absent": 1, "excused": 1},
        )
        # The client derives the rate from these counts, so a zeroed or
        # partial row would silently read as 0% attendance.
        self.assertTrue(all(row[k] for k in ("name", "lrn", "subject", "month")))

    def test_registrar_ships_staff_demographics_with_its_own_view(self):
        self._login("reg-demo@tass.test", "registrar")
        resp = self.client.get(reverse("dashboards:registrar"))
        payload = resp.context["dashboard_json"]
        self.assertIn("staffDemographics", payload)
        rows = {r["code"]: r for r in payload["staffDemographics"]["rows"]}
        self.assertIn("EMP-101", rows)
        self.assertEqual(rows["EMP-101"]["gender"], "Female")
        self.assertEqual(rows["EMP-101"]["birth"], "1990-05-04")
        self.assertContains(resp, 'id="view-staffdemographics"')
        self.assertContains(resp, "showView('staffdemographics')")

    def test_subject_coverage_defines_the_helper_it_calls(self):
        # validateSubjectCoverage() and autoEnrollVC() both call
        # requiredSubjects(); when it was never defined the coverage page threw
        # a ReferenceError and rendered nothing.
        self._login("reg-cov@tass.test", "registrar")
        resp = self.client.get(reverse("dashboards:registrar"))
        self.assertContains(resp, "function requiredSubjects(grade)")
        self.assertContains(resp, "window.requiredSubjects=requiredSubjects")

    def test_payload_ships_every_key_its_bridge_reads(self):
        # The registrar page reads its tables out of one `academic-backend-data`
        # payload and bails to an empty state as soon as a key is absent. Pin
        # the keys the bridge destructures so a future refactor cannot silently
        # blank every registrar screen again.
        self._login("reg-keys@tass.test", "registrar")
        payload = self.client.get(reverse("dashboards:registrar")).context[
            "dashboard_json"
        ]
        for key in (
            "students", "attendance", "staffDemographics", "demographics",
            "sections", "subjects",
        ):
            self.assertIn(key, payload)


class CoordinatorPanelPayloadTests(TestCase):
    """Performance, Attendance and Announcements are three separate views in
    academic_coordinator.html that used to be shipped an empty payload. Each
    view bails to a "no data yet" state as soon as one of its keys is missing
    from `academic-backend-data`, so the same JS that renders a real payload
    silently drew blank panels. These tests pin the key names and row shapes
    the renderers actually destructure."""

    @classmethod
    def setUpTestData(cls):
        import datetime

        from apps.accounts.models import EmployeeProfile, StudentProfile
        from apps.academic_structure.models import (
            AcademicYear, Section, Subject,
        )
        from apps.attendance.models import AttendanceRecord, AttendanceSession
        from apps.virtual_classrooms.models import (
            VirtualClassroom, VirtualClassroomEnrollment,
        )

        cls.teacher = User.objects.create_user(
            email="coord-faculty@tass.test", password="pass12345x"
        )
        cls.teacher.status = "active"
        cls.teacher.save()
        EmployeeProfile.objects.create(
            user=cls.teacher,
            employee_number="EMP-COORD-1",
            first_name="Lorenzo",
            last_name="Reyes",
            sex="M",
            birthdate=datetime.date(1988, 2, 11),
        )
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2027, 5, 31),
            status="ACTIVE",
        )
        section = Section.objects.create(
            academic_year=cls.year, name="Bonifacio", grade_level=9
        )
        subject = Subject.objects.create(
            academic_year=cls.year, name="Mathematics", grade_level=9
        )
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year, section=section, subject=subject,
            teacher=cls.teacher,
        )
        cls.student_user = User.objects.create_user(
            email="136100100002@stu.test", password="pass12345x"
        )
        cls.student = StudentProfile.objects.create(
            user=cls.student_user,
            lrn="136100100002",
            first_name="Jose",
            last_name="Mercado",
            sex="M",
            birthdate=datetime.date(2012, 4, 2),
        )
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=cls.vc, student=cls.student,
        )
        # Half present, half absent -> 50% attendance, which must read
        # "At Risk" in both table renderers.
        for day, status in (
            (10, AttendanceRecord.PRESENT_ON_TIME),
            (11, AttendanceRecord.ABSENT_UNEXCUSED),
        ):
            session = AttendanceSession.objects.create(
                virtual_classroom=cls.vc,
                session_date=datetime.date(2026, 6, day),
                created_by=cls.teacher,
            )
            AttendanceRecord.objects.create(
                session=session, student=cls.student, status=status,
            )

    def _login(self, role, tag):
        user = User.objects.create_user(
            email="coord-{}-{}@tass.test".format(tag, role),
            password="pass12345x",
        )
        user.status = "active"
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        self.client.login(username=user.email, password="pass12345x")
        return user

    def test_panels_render_real_rows(self):
        self._login("assistant_principal", "alpha")
        resp = self.client.get(reverse("dashboards:academic_coordinator"))
        self.assertEqual(resp.status_code, 200)
        payload = resp.context["dashboard_json"]
        for key in ("performance", "attendanceRows", "announcements"):
            self.assertIn(key, payload)
            self.assertIsInstance(payload[key], list)

        perf = payload["performance"]
        self.assertEqual(len(perf), 1, perf)
        self.assertEqual(
            {k: perf[0][k] for k in ("student", "cls", "teacher")},
            {
                "student": "Jose Mercado",
                "cls": "G9-Bonifacio",
                "teacher": "Lorenzo Reyes",
            },
        )
        # No graded work exists yet, so these must stay empty rather than read
        # 0 and surface as "At Risk".
        self.assertIsNone(perf[0]["avg"])
        self.assertIsNone(perf[0]["sub"])
        self.assertEqual(perf[0]["att"], 50.0)

        att = payload["attendanceRows"]
        self.assertEqual(len(att), 1, att)
        self.assertEqual(att[0]["cls"], "G9-Bonifacio")
        self.assertEqual(att[0]["status"], "At Risk")
        self.assertEqual(att[0]["avg"], 50.0)

        self.assertEqual(payload["announcements"], [])

    def test_frontend_reads_those_key_names(self):
        # The wire names here are the whole point: `attendanceRows` (not
        # `attendance`) is what the coordinator template reads, while several
        # other dashboards use the narrower name for a different kind of row.
        self._login("assistant_principal", "beta")
        resp = self.client.get(reverse("dashboards:academic_coordinator"))
        self.assertContains(resp, "data.performance")
        self.assertContains(resp, "data.attendanceRows")
        self.assertContains(resp, "data.announcements")


class StudentAnnouncementTargetTests(TestCase):
    """The student dashboard anchors every announcement row by the raw
    announcement id, so a bell notification can open the exact one in-page.
    The payloads must expose `announcement_id`, kept separate from the
    school row's prefixed display id and the class row's `key`, because the
    notification's ``source_id`` is the bare primary key."""

    @classmethod
    def setUpTestData(cls):
        import datetime

        from apps.accounts.models import StudentProfile
        from apps.academic_structure.models import AcademicYear, Section, Subject
        from apps.communication.models import ClassAnnouncement, SchoolAnnouncement
        from apps.virtual_classrooms.models import (
            VirtualClassroom, VirtualClassroomEnrollment,
        )

        cls.year = AcademicYear.objects.create(
            label="2026-SA",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2027, 5, 31),
            status="ACTIVE",
        )
        cls.teacher = User.objects.create_user(
            email="sa_faculty@tass.test", password="pass12345x"
        )
        cls.teacher.status = "active"
        cls.teacher.save()
        section = Section.objects.create(
            academic_year=cls.year, name="SA", grade_level=9
        )
        subject = Subject.objects.create(
            academic_year=cls.year, name="Science", grade_level=9
        )
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year, section=section, subject=subject,
            teacher=cls.teacher,
        )
        cls.student_user = User.objects.create_user(
            email="sa_student@tass.test", password="pass12345x"
        )
        cls.student_user.status = "active"
        cls.student_user.save()
        cls.student_user.groups.add(Group.objects.get_or_create(name="student")[0])
        cls.profile = StudentProfile.objects.create(
            user=cls.student_user,
            lrn="136100100099",
            first_name="Ana",
            last_name="Cruz",
            sex="F",
            birthdate=datetime.date(2012, 1, 1),
        )
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=cls.vc,
            student=cls.profile,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        )
        cls.class_ann = ClassAnnouncement.objects.create(
            virtual_classroom=cls.vc, posted_by=cls.teacher, content="Bring lab coats"
        )
        cls.school_ann = SchoolAnnouncement.objects.create(
            title="Foundation Day", content="Parade", posted_by=cls.teacher
        )

    def _payload(self):
        from apps.dashboards.context import student_context

        return student_context(self.student_user)["dashboard_json"]

    def test_school_announcement_rows_expose_raw_id(self):
        rows = self._payload()["schoolAnnouncements"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["announcement_id"], self.school_ann.pk)

    def test_class_announcement_rows_expose_raw_id(self):
        rows = self._payload()["announcements_by_classroom"][str(self.vc.pk)]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["announcement_id"], self.class_ann.pk)


class RegistrarCalendarWriteTests(TestCase):
    """The Registrar keeps the Calendar of Activities up to date: the dashboard
    payload has to say so (the front end gates the Add/Edit controls on it), and
    the write endpoint has to let the role through while read-only roles stay
    read-only."""

    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)

    def _login(self, email, role):
        user = User.objects.create_user(email=email, password="pass12345x")
        user.status = "active"
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role)[0])
        self.client.login(username=email, password="pass12345x")
        return user

    def test_registrar_payload_marks_the_calendar_editable(self):
        from apps.dashboards.context import _calendar_payload

        user = self._login("cal-reg@tass.test", "registrar")
        self.assertTrue(_calendar_payload(user)["canEdit"])

        payload = self.client.get(reverse("dashboards:registrar")).context[
            "dashboard_json"
        ]
        self.assertTrue(payload["calendar"]["canEdit"])

    def test_registrar_can_create_a_school_activity(self):
        from apps.communication.models import SchoolEvent

        self._login("cal-reg2@tass.test", "registrar")
        res = self.client.post(
            reverse("api_calendar"),
            data=json.dumps(
                {
                    "title": "Foundation Week",
                    "startDate": "2026-10-12",
                    "category": "event",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(SchoolEvent.objects.filter(title="Foundation Week").exists())

    def test_a_read_only_role_cannot_write_activities(self):
        self._login("cal-stu@tass.test", "student")
        res = self.client.post(
            reverse("api_calendar"),
            data=json.dumps({"title": "Nope", "startDate": "2026-10-12"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 403)
