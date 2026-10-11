from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError
from django.test import TestCase

from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.virtual_classrooms.forms import VirtualClassroomForm
from apps.virtual_classrooms.models import VirtualClassroom
from apps.virtual_classrooms.services import (
    pick_section_teacher,
    teacher_conflict_in_section,
)

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


class OneTeacherPerClassroomTestCase(TestCase):
    """A student must never see the same teacher twice in their classroom list,
    which means no teacher may hold two virtual classrooms in one section."""

    def setUp(self):
        self.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 3, 31),
        )
        self.section = Section.objects.create(
            academic_year=self.year, name="STEM-Newton", grade_level=11
        )
        self.other_section = Section.objects.create(
            academic_year=self.year, name="STEM-Einstein", grade_level=11
        )
        self.subject = Subject.objects.create(
            academic_year=self.year,
            name="Elective A",
            grade_level=11,
            subject_type=Subject.TYPE_ELECTIVE,
        )
        self.subject_two = Subject.objects.create(
            academic_year=self.year,
            name="Elective B",
            grade_level=11,
            subject_type=Subject.TYPE_ELECTIVE,
        )
        self.teacher = User.objects.create_user(email="teacher@test.ph", password="pass")
        self.other_teacher = User.objects.create_user(
            email="teacher2@test.ph", password="pass"
        )
        for t in (self.teacher, self.other_teacher):
            t.groups.add(Group.objects.get_or_create(name="teacher")[0])

    def test_teacher_may_teach_the_same_subject_in_another_section(self):
        VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
        )
        # Same teacher, same subject, different section - a student is only ever
        # enrolled in one section, so this is safe.
        VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.other_section,
            subject=self.subject,
            teacher=self.teacher,
        )
        self.assertEqual(VirtualClassroom.objects.count(), 2)

    def test_form_rejects_a_teacher_already_teaching_in_that_section(self):
        VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
        )
        form = VirtualClassroomForm(
            data={
                "academic_year": self.year.pk,
                "section": self.section.pk,
                "subject": self.subject_two.pk,
                "teacher": self.teacher.pk,
                "grading_period": "",
                "theme_color": "#4F46E5",
                "status": VirtualClassroom.STATUS_ACTIVE,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("teacher", form.errors)

    def test_form_allows_a_free_teacher_in_the_same_section(self):
        VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
        )
        form = VirtualClassroomForm(
            data={
                "academic_year": self.year.pk,
                "section": self.section.pk,
                "subject": self.subject_two.pk,
                "teacher": self.other_teacher.pk,
                "grading_period": "",
                "theme_color": "#4F46E5",
                "status": VirtualClassroom.STATUS_ACTIVE,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_pick_section_teacher_prefers_the_teacher_when_free(self):
        vc = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
        )
        self.assertEqual(
            pick_section_teacher(self.section, preferred=self.other_teacher), self.other_teacher
        )
        self.assertIsNone(teacher_conflict_in_section(self.section, self.teacher, vc))
        self.assertIsNotNone(teacher_conflict_in_section(self.section, self.teacher))

    def test_pick_section_teacher_never_returns_a_teacher_already_in_the_section(self):
        VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
        )
        taken = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject_two,
            teacher=self.other_teacher,
        )
        third = pick_section_teacher(self.section, exclude_classroom=taken)
        self.assertNotIn(third, (self.teacher, self.other_teacher))
