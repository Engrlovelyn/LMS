from datetime import date

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.virtual_classrooms.models import VirtualClassroom

User = get_user_model()


class VirtualClassroomConstraintTestCase(TestCase):
    def setUp(self):
        self.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 3, 31),
        )
        self.section = Section.objects.create(
            academic_year=self.year,
            name="STEM-Newton",
            grade_level=11,
        )
        self.subject = Subject.objects.create(
            academic_year=self.year,
            name="Elective A",
            grade_level=11,
            subject_type=Subject.TYPE_ELECTIVE,
        )
        self.teacher = User.objects.create_user(email="teacher@test.ph", password="pass")

    def test_distinct_grading_periods_allowed(self):
        vc1 = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
            grading_period="1ST_TRIMESTER",
        )
        vc2 = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
            grading_period="2ND_TRIMESTER",
        )
        self.assertEqual(VirtualClassroom.objects.count(), 2)
        self.assertNotEqual(vc1.pk, vc2.pk)

    def test_duplicate_subject_section_year_period_rejected(self):
        VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
            grading_period="1ST_TRIMESTER",
        )
        with self.assertRaises(IntegrityError):
            VirtualClassroom.objects.create(
                academic_year=self.year,
                section=self.section,
                subject=self.subject,
                teacher=self.teacher,
                grading_period="1ST_TRIMESTER",
            )