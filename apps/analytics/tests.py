import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.assessments.models import Assessment, AssessmentAssignment, Submission
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .models import GeneratedReport


class SubmissionReportGenerationTests(TestCase):
    """Generating a Submission & Assessment Report persists a downloadable CSV."""

    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026 PA",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            status="ACTIVE",
        )
        cls.section = Section.objects.create(
            academic_year=cls.year, name="PA - A", grade_level=9
        )
        cls.subject = Subject.objects.create(
            academic_year=cls.year, name="English", grade_level=9
        )
        cls.admin = User.objects.create_superuser(
            email="rep_admin@tass.local", password="pw"
        )
        cls.teacher = User.objects.create_user(
            email="rep_teacher@tass.local", password="pw"
        )
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section,
            subject=cls.subject,
            teacher=cls.teacher,
        )
        student_user = User.objects.create_user(
            email="rep_student@tass.local", password="pw"
        )
        cls.student = StudentProfile.objects.create(
            user=student_user,
            lrn="LRN-2026-7001",
            first_name="Mara",
            last_name="Reyes",
            birthdate=datetime.date(2009, 3, 3),
            sex="F",
        )
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=cls.vc, student=cls.student
        )
        cls.assessment = Assessment.objects.create(
            subject=cls.subject,
            academic_year=cls.year,
            created_by=cls.teacher,
            title="Reading Test",
            assessment_method=Assessment.METHOD_EXAMINATION,
            assessment_type=Assessment.TYPE_SUMMATIVE_TEST,
            status=Assessment.STATUS_ACTIVE,
            submission_deadline=timezone.now() + datetime.timedelta(days=3),
        )
        cls.assignment = AssessmentAssignment.objects.create(
            assessment=cls.assessment, virtual_classroom=cls.vc, assigned_by=cls.teacher
        )
        cls.submission = Submission.objects.create(
            assessment_assignment=cls.assignment, student=cls.student
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_reports_page_renders(self):
        res = self.client.get(reverse("analytics:submission_reports"))
        self.assertEqual(res.status_code, 200)

    def test_generate_creates_persisted_downloadable_report(self):
        res = self.client.post(reverse("analytics:submission_report_generate"))
        self.assertEqual(res.status_code, 302)
        report = GeneratedReport.objects.get(
            report_type=GeneratedReport.TYPE_SUBMISSION_REPORT
        )
        self.assertEqual(report.generated_by, self.admin)
        self.assertEqual(report.academic_year, self.year)
        self.assertTrue(report.file.name)
        content = report.file.read().decode("utf-8")
        self.assertIn("Reading Test", content)
        self.assertIn("Mara", content)

    def test_report_download_serves_the_csv(self):
        self.client.post(reverse("analytics:submission_report_generate"))
        report = GeneratedReport.objects.get(
            report_type=GeneratedReport.TYPE_SUBMISSION_REPORT
        )
        res = self.client.get(
            reverse("analytics:report_download", args=[report.pk])
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(".csv", res["Content-Disposition"])
        body = b"".join(res.streaming_content).decode("utf-8")
        self.assertIn("Reading Test", body)
