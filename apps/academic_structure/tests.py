from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.assessments.models import Assessment, AssessmentAssignment
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.accounts.models import EmployeeProfile, StudentProfile
from apps.communication.models import ClassAnnouncement
from apps.teaching_learning.models import LearningResource, LearningResourcePlacement, Topic
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .models import AcademicStructureVersion, AcademicYear, Section, StudentEnrollment, Subject
from .services import rollover_year

User = get_user_model()


class VirtualClassroomEnrollmentTestCase(TestCase):
    def setUp(self):
        self.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 3, 31),
        )
        self.section = Section.objects.create(
            academic_year=self.year,
            name="Newton",
            grade_level=8,
        )
        self.subject = Subject.objects.create(
            academic_year=self.year,
            name="Science",
            grade_level=8,
        )
        self.teacher = User.objects.create_user(email="teacher@test.ph", password="pass")
        self.classroom = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
            status=VirtualClassroom.STATUS_ACTIVE,
        )
        self.user = User.objects.create_user(email="student@test.ph", password="pass")

        self.student = StudentProfile.objects.create(
            user=self.user,
            lrn="123456789012",
            first_name="Juan",
            last_name="Dela Cruz",
            birthdate=date(2008, 1, 1),
        )

    def test_active_enrollment_created(self):
        enrollment = VirtualClassroomEnrollment.objects.create(
            virtual_classroom=self.classroom,
            student=self.student,
        )
        self.assertEqual(enrollment.status, VirtualClassroomEnrollment.STATUS_ACTIVE)

    def test_duplicate_active_enrollment_rejected(self):
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=self.classroom,
            student=self.student,
        )
        with self.assertRaises(IntegrityError):
            VirtualClassroomEnrollment.objects.create(
                virtual_classroom=self.classroom,
                student=self.student,
            )

    def test_reenroll_after_removal_allowed(self):
        first = VirtualClassroomEnrollment.objects.create(
            virtual_classroom=self.classroom,
            student=self.student,
        )
        first.status = VirtualClassroomEnrollment.STATUS_REMOVED
        first.save()
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=self.classroom,
            student=self.student,
        )
        self.assertEqual(
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=self.classroom,
                student=self.student,
                status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            ).count(),
            1,
        )


class AcademicYearApiTestCase(TestCase):
    """POST /api/academic-year/ and POST /api/structure/ creation endpoints."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group

        group, _ = Group.objects.get_or_create(name="system_admin")
        cls.admin = get_user_model().objects.create_user(
            email="sa@tass.test", password="pass", is_staff=True,
            status=get_user_model().Status.ACTIVE,
        )
        cls.admin.groups.add(group)

    def setUp(self):
        self.client.force_login(self.admin)

    def _post(self, url, payload):
        return self.client.post(url, payload, content_type="application/json")

    def test_create_year_archives_previous_active(self):
        older = AcademicYear.objects.create(
            label="2025-2026", start_date=date(2025, 6, 1), end_date=date(2026, 3, 31)
        )
        res = self._post("/api/academic-year/", {
            "action": "create", "label": "2026-2027",
            "start": "2026-06-01", "end": "2027-03-31",
        })
        body = res.json()
        self.assertEqual(res.status_code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["active"], "2026-2027")
        older.refresh_from_db()
        self.assertEqual(older.status, AcademicYear.STATUS_ARCHIVED)
        labels = {y["label"]: y["status"] for y in body["years"]}
        self.assertEqual(labels["2026-2027"], "Active")
        # A structure version defaults to Quarter when none is supplied.
        new_year = AcademicYear.objects.get(label="2026-2027")
        self.assertEqual(
            new_year.structure_version.grading_period_type,
            AcademicStructureVersion.QUARTER,
        )

    def test_create_year_with_grading_structure(self):
        res = self._post("/api/academic-year/", {
            "action": "create", "label": "2026-2027",
            "start": "2026-06-01", "end": "2027-03-31",
            "gradingPeriodType": "trimester",
        })
        self.assertEqual(res.status_code, 200, res.json())
        new_year = AcademicYear.objects.get(label="2026-2027")
        self.assertEqual(
            new_year.structure_version.grading_period_type,
            AcademicStructureVersion.TRIMESTER,
        )
        row = next(y for y in res.json()["years"] if y["label"] == "2026-2027")
        self.assertEqual(row["gradingPeriod"]["code"], "TRIMESTER")

    def test_set_grading_action(self):
        year = AcademicYear.objects.create(
            label="2026-2027", start_date=date(2026, 6, 1), end_date=date(2027, 3, 31)
        )
        res = self._post("/api/academic-year/", {
            "action": "grading", "id": year.pk, "gradingPeriodType": "SEMESTER",
        })
        self.assertEqual(res.status_code, 200, res.json())
        year.refresh_from_db()
        self.assertEqual(
            year.structure_version.grading_period_type,
            AcademicStructureVersion.SEMESTER,
        )

    def test_create_year_rejects_duplicate_label_and_bad_dates(self):
        AcademicYear.objects.create(
            label="2026-2027", start_date=date(2026, 6, 1), end_date=date(2027, 3, 31)
        )
        dup = self._post("/api/academic-year/", {
            "action": "create", "label": "2026-2027",
            "start": "2026-06-01", "end": "2027-03-31",
        })
        self.assertEqual(dup.status_code, 400)
        bad = self._post("/api/academic-year/", {
            "action": "create", "label": "2027-2028",
            "start": "2027-06-01", "end": "2027-01-01",
        })
        self.assertEqual(bad.status_code, 400)

    def test_activate_and_archive_actions(self):
        a = AcademicYear.objects.create(
            label="2021-2022", start_date=date(2021, 6, 1), end_date=date(2022, 3, 31),
            status=AcademicYear.STATUS_ARCHIVED,
        )
        b = AcademicYear.objects.create(
            label="2022-2023", start_date=date(2022, 6, 1), end_date=date(2023, 3, 31),
            status=AcademicYear.STATUS_ARCHIVED,
        )
        res = self._post("/api/academic-year/", {"action": "activate", "id": b.pk})
        self.assertEqual(res.json()["active"], "2022-2023")
        res = self._post("/api/academic-year/", {"action": "archive", "id": a.pk})
        self.assertTrue(res.json()["ok"])
        res = self._post("/api/academic-year/", {"action": "archive", "id": b.pk})
        self.assertEqual(res.status_code, 400)  # cannot archive the active year

    def test_create_section_and_subject_for_active_year(self):
        year = AcademicYear.objects.create(
            label="2026-2027", start_date=date(2026, 6, 1), end_date=date(2027, 3, 31)
        )
        sec = self._post("/api/structure/", {"kind": "section", "grade": 8, "name": "Newton"})
        self.assertEqual(sec.status_code, 200, sec.json())
        self.assertTrue(Section.objects.filter(academic_year=year, name="Newton", grade_level=8).exists())
        dup = self._post("/api/structure/", {"kind": "section", "grade": 8, "name": "Newton"})
        self.assertEqual(dup.status_code, 400)

        subj = self._post("/api/structure/", {
            "kind": "subject", "grade": 8, "name": "General Biology 1",
            "type": "APPLIED", "period": "1ST_TRIMESTER",
        })
        self.assertEqual(subj.status_code, 200, subj.json())
        self.assertTrue(Subject.objects.filter(
            academic_year=year, name="General Biology 1", grade_level=8
        ).exists())
        unknown = self._post("/api/structure/", {"kind": "classroom"})
        self.assertEqual(unknown.status_code, 400)

    def test_structure_needs_active_year(self):
        AcademicYear.objects.filter().delete()
        res = self._post("/api/structure/", {"kind": "section", "grade": 8, "name": "Newton"})
        self.assertEqual(res.status_code, 409)


class ClassroomDetailApiTestCase(TestCase):
    """GET /api/classroom/<id>/ — the admin viewer's read-only aggregate."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group

        admin_group, _ = Group.objects.get_or_create(name="system_admin")
        cls.admin = get_user_model().objects.create_user(
            email="sa@tass.test", password="pass", is_staff=True,
            status=get_user_model().Status.ACTIVE,
        )
        cls.admin.groups.add(admin_group)
        cls.teacher = get_user_model().objects.create_user(
            email="teacher@tass.test", password="pass",
            status=get_user_model().Status.ACTIVE,
        )
        EmployeeProfile.objects.create(
            user=cls.teacher, employee_number="T-001",
            first_name="Maria", last_name="Santos",
        )
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 3, 31),
        )
        cls.section = Section.objects.create(
            academic_year=cls.year, name="Newton", grade_level=8,
        )
        cls.subject = Subject.objects.create(
            academic_year=cls.year, name="Science", grade_level=8,
        )
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year, section=cls.section,
            subject=cls.subject, teacher=cls.teacher,
            grading_period="1ST_TRIMESTER", theme_color="#0EA5E9",
        )

        cls.students = []
        for i, (first, last) in enumerate([("Ana", "Reyes"), ("Ben", "Cruz")], start=1):
            user = get_user_model().objects.create_user(
                email="s{:02d}@tass.test".format(i), password="pass",
                status=get_user_model().Status.ACTIVE,
            )
            profile = StudentProfile.objects.create(
                user=user, lrn="{:012d}".format(i), first_name=first, last_name=last,
                birthdate=date(2012, 1, 15), sex="F" if i == 1 else "M",
            )
            cls.students.append(profile)
            VirtualClassroomEnrollment.objects.create(virtual_classroom=cls.vc, student=profile)

        # Attendance: one session, one present + one absent record.
        session = AttendanceSession.objects.create(
            virtual_classroom=cls.vc, session_date=timezone.localdate(),
            created_by=cls.teacher,
        )
        AttendanceRecord.objects.create(
            session=session, student=cls.students[0], status=AttendanceRecord.PRESENT_ON_TIME,
        )
        AttendanceRecord.objects.create(
            session=session, student=cls.students[1], status=AttendanceRecord.ABSENT_UNEXCUSED,
        )

        # A topic with one placed link resource.
        cls.topic = Topic.objects.create(virtual_classroom=cls.vc, name="Motion", order=1)
        resource = LearningResource.objects.create(
            subject=cls.subject, academic_year=cls.year, uploaded_by=cls.teacher,
            title="Kinematics Notes", resource_type=LearningResource.TYPE_LINK,
            external_link="https://example.com/notes",
        )
        LearningResourcePlacement.objects.create(
            resource=resource, virtual_classroom=cls.vc, topic=cls.topic,
            placed_by=cls.teacher,
        )

        # One assessment assigned, with a pending submission.
        assessment = Assessment.objects.create(
            subject=cls.subject, academic_year=cls.year, created_by=cls.teacher,
            title="Summative Test 1", assessment_method=Assessment.METHOD_EXAMINATION,
            assessment_type=Assessment.TYPE_SUMMATIVE_TEST,
            submission_deadline=timezone.now() + timezone.timedelta(days=7),
        )
        cls.assignment = AssessmentAssignment.objects.create(
            assessment=assessment, virtual_classroom=cls.vc, assigned_by=cls.teacher,
        )

        # One class announcement.
        ClassAnnouncement.objects.create(
            virtual_classroom=cls.vc, posted_by=cls.teacher,
            content="Reminder: bring your calculators tomorrow.",
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def _get(self, pk):
        return self.client.get("/api/classroom/{}/".format(pk))

    def test_opens_classroom_with_full_contents(self):
        res = self._get(self.vc.pk)
        body = res.json()
        self.assertEqual(res.status_code, 200, body)
        self.assertTrue(body["ok"])
        vc = body["virtualClassroom"]
        self.assertEqual(vc["name"], "Science")
        self.assertEqual(vc["section"], "Newton")
        self.assertEqual(vc["teacher"], "Maria Santos")
        self.assertEqual(vc["status"], "Active")
        self.assertEqual(vc["year"], "2026-2027")
        self.assertEqual(vc["period"], "1ST_TRIMESTER")
        self.assertEqual(vc["themeColor"], "#0EA5E9")

        self.assertEqual(len(body["students"]), 2)
        names = {s["lrn"] for s in body["students"]}
        self.assertEqual(names, {"000000000001", "000000000002"})
        student = next(s for s in body["students"] if s["lrn"] == "000000000001")
        self.assertEqual(student["present"], 1)
        self.assertEqual(student["absent"], 0)

        self.assertEqual([t["name"] for t in body["topics"]], ["Motion"])
        self.assertEqual(len(body["resources"]), 1)
        self.assertEqual(body["resources"][0]["title"], "Kinematics Notes")
        self.assertEqual(body["resources"][0]["topic"], "Motion")
        self.assertEqual(body["resources"][0]["externalLink"], "https://example.com/notes")

        self.assertEqual(len(body["assessments"]), 1)
        assessment = body["assessments"][0]
        self.assertEqual(assessment["title"], "Summative Test 1")
        self.assertEqual(assessment["students"], 2)
        self.assertEqual(assessment["submitted"], 0)
        self.assertEqual(assessment["graded"], 0)

        self.assertEqual(len(body["announcements"]), 1)
        self.assertIn("calculators", body["announcements"][0]["content"])
        self.assertEqual(body["announcements"][0]["postedBy"], "Maria Santos")

        attendance = body["attendance"]
        self.assertEqual(attendance["sessions"], 1)
        self.assertEqual(attendance["present"], 1)
        self.assertEqual(attendance["absent"], 1)
        self.assertEqual(attendance["recent"][0]["total"], 2)

    def test_missing_classroom_returns_404(self):
        res = self._get(999999)
        self.assertEqual(res.status_code, 404)

    def test_non_admin_role_is_denied(self):
        student_user = self.students[0].user
        self.client.force_login(student_user)
        res = self._get(self.vc.pk)
        self.assertEqual(res.status_code, 403)

class YearRolloverTestCase(TestCase):
    """apps.academic_structure.services.rollover_year + the API rollover action."""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group, Permission

        group, _ = Group.objects.get_or_create(name="system_admin")
        # The Django pages guard on the permission codename, which the RBAC
        # sync command normally grants to this group.
        group.permissions.add(
            Permission.objects.get(codename="rollover_academic_year")
        )
        cls.admin = get_user_model().objects.create_user(
            email="sa@tass.test", password="pass", is_staff=True,
            status=get_user_model().Status.ACTIVE,
        )
        cls.admin.groups.add(group)
        cls.teacher = get_user_model().objects.create_user(
            email="teacher@tass.test", password="pass",
            status=get_user_model().Status.ACTIVE,
        )
        cls.previous = AcademicYear.objects.create(
            label="2025-2026", start_date=date(2025, 6, 1), end_date=date(2026, 3, 31),
            status=AcademicYear.STATUS_ARCHIVED,
        )
        cls.current = AcademicYear.objects.create(
            label="2026-2027", start_date=date(2026, 6, 1), end_date=date(2027, 3, 31),
        )
        cls.section = Section.objects.create(
            academic_year=cls.previous, name="Newton", grade_level=8, adviser=cls.teacher,
        )
        cls.subject = Subject.objects.create(
            academic_year=cls.previous, name="General Biology 1",
            grade_level=8, grading_period="1ST_TRIMESTER",
        )
        cls.classroom = VirtualClassroom.objects.create(
            academic_year=cls.previous, section=cls.section, subject=cls.subject,
            teacher=cls.teacher, grading_period="1ST_TRIMESTER",
            status=VirtualClassroom.STATUS_ACTIVE,
        )

    def test_copies_structure_and_keeps_adviser_and_teacher(self):
        report = rollover_year(self.previous, self.current)
        self.assertEqual((report.sections, report.subjects, report.classes), (1, 1, 1))
        self.assertEqual(report.skipped, [])

        section = Section.objects.get(academic_year=self.current, name="Newton")
        self.assertEqual(section.grade_level, 8)
        self.assertEqual(section.adviser, self.teacher)
        self.assertEqual(
            Subject.objects.get(academic_year=self.current, name="General Biology 1").grading_period,
            "1ST_TRIMESTER",
        )
        classroom = VirtualClassroom.objects.get(academic_year=self.current)
        self.assertEqual(classroom.section, section)
        self.assertEqual(classroom.teacher, self.teacher)
        # The source year is left untouched.
        self.assertEqual(Section.objects.filter(academic_year=self.previous).count(), 1)

    def test_second_run_reports_skips_instead_of_failing(self):
        rollover_year(self.previous, self.current)
        report = rollover_year(self.previous, self.current)
        self.assertEqual(report.copied, 0)
        self.assertEqual(len(report.skipped), 3)
        self.assertEqual(Section.objects.filter(academic_year=self.current).count(), 1)
        self.assertEqual(VirtualClassroom.objects.filter(academic_year=self.current).count(), 1)

    def test_rejects_same_year_and_archived_target(self):
        with self.assertRaises(ValidationError):
            rollover_year(self.previous, self.previous)
        with self.assertRaises(ValidationError):
            rollover_year(self.current, self.previous)

    def test_api_rollover_into_active_year(self):
        self.client.force_login(self.admin)
        res = self.client.post(
            "/api/academic-year/",
            {"action": "rollover", "sourceId": self.previous.pk},
            content_type="application/json",
        )
        body = res.json()
        self.assertEqual(res.status_code, 200, body)
        self.assertEqual(body["rollover"]["source"], "2025-2026")
        self.assertEqual(body["rollover"]["target"], "2026-2027")
        self.assertEqual(body["rollover"]["sections"], 1)
        row = next(y for y in body["years"] if y["label"] == "2026-2027")
        self.assertEqual(row["sections"], 1)
        self.assertEqual(row["classrooms"], 1)

    def test_api_rollover_rejects_unknown_source(self):
        self.client.force_login(self.admin)
        res = self.client.post(
            "/api/academic-year/",
            {"action": "rollover", "sourceId": 999999},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)

    def test_rollover_page_offers_the_active_year_as_target(self):
        self.client.force_login(self.admin)
        res = self.client.get(f"/academic/years/{self.previous.pk}/rollover/")
        self.assertEqual(res.status_code, 200)
        form = res.context["form"]
        self.assertEqual(
            [y.label for y in form.fields["target_academic_year"].queryset],
            ["2026-2027"],
        )
        self.assertContains(res, "Source Classrooms")
    def test_rollover_page_copies_into_the_chosen_year(self):
        self.client.force_login(self.admin)
        res = self.client.post(
            f"/academic/years/{self.previous.pk}/rollover/",
            {"target_academic_year": self.current.pk},
        )
        self.assertEqual(res.status_code, 302)
        self.assertTrue(Section.objects.filter(academic_year=self.current, name="Newton").exists())


class StudentEnrollmentUpdateTestCase(TestCase):
    """Update Student Enrollment -- move a learner to another section / archive."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(
            email="sa@tass.test", password="pass"
        )
        cls.admin.status = get_user_model().Status.ACTIVE
        cls.admin.save()
        cls.year = AcademicYear.objects.create(
            label="2026-2027", start_date=date(2026, 6, 1), end_date=date(2027, 3, 31),
        )
        cls.section_a = Section.objects.create(
            academic_year=cls.year, name="Newton", grade_level=8,
        )
        cls.section_b = Section.objects.create(
            academic_year=cls.year, name="Einstein", grade_level=8,
        )
        user = get_user_model().objects.create_user(
            email="student@tass.test", password="pass",
        )
        cls.student = StudentProfile.objects.create(
            user=user, lrn="123456789099", first_name="Juan", last_name="Dela Cruz",
            birthdate=date(2008, 1, 1),
        )
        cls.enrollment = StudentEnrollment.objects.create(
            student=cls.student, academic_year=cls.year, section=cls.section_a,
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_move_student_to_another_section(self):
        res = self.client.post(
            reverse(
                "academic:student_enrollment_update",
                args=[self.section_a.pk, self.enrollment.pk],
            ),
            {"section": self.section_b.pk, "status": StudentEnrollment.STATUS_ENROLLED},
        )
        self.assertEqual(res.status_code, 302)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.section, self.section_b)
        self.assertEqual(self.enrollment.status, StudentEnrollment.STATUS_ENROLLED)

    def test_archive_enrollment_sets_archived_at(self):
        res = self.client.post(
            reverse(
                "academic:student_enrollment_update",
                args=[self.section_a.pk, self.enrollment.pk],
            ),
            {"section": self.section_a.pk, "status": StudentEnrollment.STATUS_ARCHIVED},
        )
        self.assertEqual(res.status_code, 302)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.status, StudentEnrollment.STATUS_ARCHIVED)
        self.assertIsNotNone(self.enrollment.archived_at)
