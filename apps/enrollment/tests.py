import datetime

from django.apps import apps as django_apps
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment


class EnrollmentAppTests(TestCase):
    def test_app_registered(self):
        config = django_apps.get_app_config("enrollment")
        self.assertEqual(config.name, "apps.enrollment")
        self.assertEqual(config.label, "enrollment")

    def test_urls_load(self):
        self.assertTrue(reverse("enrollment:dashboard"))


class RemoveStudentTests(TestCase):
    """Regression: remove_student must archive the enrollment without crashing.

    The view previously referenced an undefined ``classroom`` local when
    notifying the student, raising NameError after the status update.
    """

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
            email="enroll_admin@tass.local", password="pw"
        )
        cls.teacher = User.objects.create_user(
            email="enroll_teacher@tass.local", password="pw"
        )
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section,
            subject=cls.subject,
            teacher=cls.teacher,
        )
        student = User.objects.create_user(
            email="enroll_student@tass.local", password="pw"
        )
        cls.profile = StudentProfile.objects.create(
            user=student,
            lrn="LRN-2026-0001",
            first_name="Ana",
            last_name="Dela Cruz",
            birthdate=datetime.date(2009, 1, 1),
            sex="F",
        )
        cls.enrollment = VirtualClassroomEnrollment.objects.create(
            virtual_classroom=cls.vc, student=cls.profile
        )

    def test_remove_student_archives_enrollment(self):
        self.client.login(username=self.admin.email, password="pw")
        response = self.client.post(
            reverse(
                "enrollment:remove_student",
                args=[self.vc.pk, self.enrollment.pk],
            )
        )
        self.assertEqual(response.status_code, 302)
        self.enrollment.refresh_from_db()
        self.assertEqual(
            self.enrollment.status, VirtualClassroomEnrollment.STATUS_REMOVED
        )
        self.assertIsNotNone(self.enrollment.removed_at)