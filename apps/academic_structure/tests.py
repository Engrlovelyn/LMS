from datetime import date

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .models import AcademicYear, Section, Subject

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
        from apps.accounts.models import StudentProfile

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