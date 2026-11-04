import datetime

from django.test import TestCase

from apps.academic_structure.models import AcademicYear, Section, StudentEnrollment, Subject
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .models import EmployeeProfile, StudentProfile, User
from .ordering import order_students, order_teachers


class DirectoryOrderingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2027, 1, 1),
            status=AcademicYear.STATUS_ACTIVE,
        )
        cls.grade7 = Section.objects.create(
            academic_year=cls.year, name="Z Section", grade_level=7
        )
        cls.grade8 = Section.objects.create(
            academic_year=cls.year, name="A Section", grade_level=8
        )
        cls.subject7 = Subject.objects.create(
            academic_year=cls.year, name="Math 7", grade_level=7
        )
        cls.subject8 = Subject.objects.create(
            academic_year=cls.year, name="Math 8", grade_level=8
        )
        cls.vc7 = VirtualClassroom.objects.create(
            academic_year=cls.year, section=cls.grade7, subject=cls.subject7
        )
        cls.vc8 = VirtualClassroom.objects.create(
            academic_year=cls.year, section=cls.grade8, subject=cls.subject8
        )

        cls.students = {}
        records = [
            ("zulu_m", "Zulu", "M", 7),
            ("adam_m", "Adams", "M", 7),
            ("baker_f", "Baker", "F", 7),
            ("aaron_m", "Aaron", "M", 8),
        ]
        for index, (tag, surname, sex, grade) in enumerate(records, start=1):
            user = User.objects.create_user(email=f"{tag}@test.local", password="pw")
            profile = StudentProfile.objects.create(
                user=user,
                lrn=f"12345678{index:04d}",
                first_name=tag.split("_")[0].title(),
                last_name=surname,
                birthdate=datetime.date(2010, 1, 1),
                sex=sex,
            )
            section = cls.grade7 if grade == 7 else cls.grade8
            classroom = cls.vc7 if grade == 7 else cls.vc8
            StudentEnrollment.objects.create(
                student=profile, academic_year=cls.year, section=section
            )
            VirtualClassroomEnrollment.objects.create(
                student=profile, virtual_classroom=classroom
            )
            cls.students[tag] = profile

        cls.teachers = []
        for index, surname in enumerate(("Zulu", "Adams", "Baker"), start=1):
            user = User.objects.create_user(
                email=f"teacher{index}@test.local", password="pw"
            )
            EmployeeProfile.objects.create(
                user=user,
                employee_number=f"EMP{index:04d}",
                first_name=f"Teacher{index}",
                last_name=surname,
            )
            cls.teachers.append(user)

    def test_student_profiles_are_sorted_by_grade_then_surname(self):
        ordered = order_students(StudentProfile.objects.all())
        self.assertEqual(
            list(ordered.values_list("last_name", flat=True)),
            ["Adams", "Zulu", "Baker", "Aaron"],
        )

    def test_section_rosters_are_sorted_by_grade_then_surname(self):
        enrollments = order_students(
            StudentEnrollment.objects.select_related("student"), prefix="student__"
        )
        self.assertEqual(
            list(enrollments.values_list("student__last_name", flat=True)),
            ["Adams", "Zulu", "Baker", "Aaron"],
        )

    def test_virtual_classroom_rosters_are_sorted_by_grade_then_surname(self):
        enrollments = order_students(
            VirtualClassroomEnrollment.objects.select_related("student"),
            prefix="student__",
        )
        self.assertEqual(
            list(enrollments.values_list("student__last_name", flat=True)),
            ["Adams", "Zulu", "Baker", "Aaron"],
        )

    def test_teachers_are_sorted_by_employee_surname(self):
        ordered = order_teachers(User.objects.filter(pk__in=[t.pk for t in self.teachers]))
        self.assertEqual(
            list(ordered.values_list("employee_profile__last_name", flat=True)),
            ["Adams", "Baker", "Zulu"],
        )
