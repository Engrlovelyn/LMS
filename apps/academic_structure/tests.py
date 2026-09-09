from datetime import date

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from .models import AcademicYear, Section, StudentEnrollment

User = get_user_model()


class StudentEnrollmentTestCase(TestCase):
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
        self.user = User.objects.create_user(email="student@test.ph", password="pass")
        from apps.accounts.models import StudentProfile

        self.student = StudentProfile.objects.create(
            user=self.user,
            lrn="123456789012",
            first_name="Juan",
            last_name="Dela Cruz",
            birthdate=date(2008, 1, 1),
        )

    def test_active_enrollment_created(self):
        enrollment = StudentEnrollment.objects.create(
            student=self.student,
            academic_year=self.year,
            section=self.section,
        )
        self.assertEqual(enrollment.status, StudentEnrollment.STATUS_ENROLLED)

    def test_duplicate_active_enrollment_rejected(self):
        StudentEnrollment.objects.create(
            student=self.student,
            academic_year=self.year,
            section=self.section,
        )
        with self.assertRaises(IntegrityError):
            StudentEnrollment.objects.create(
                student=self.student,
                academic_year=self.year,
                section=self.section,
            )

    def test_reenroll_after_archive_allowed(self):
        first = StudentEnrollment.objects.create(
            student=self.student,
            academic_year=self.year,
            section=self.section,
        )
        first.status = StudentEnrollment.STATUS_ARCHIVED
        first.save()
        StudentEnrollment.objects.create(
            student=self.student,
            academic_year=self.year,
            section=self.section,
        )
        self.assertEqual(
            StudentEnrollment.objects.filter(
                student=self.student,
                academic_year=self.year,
                status=StudentEnrollment.STATUS_ENROLLED,
            ).count(),
            1,
        )