"""Regression tests for assigning students to a Section.

A saved Section has no roster, so the registrar needs one place to put learners
in it: add them one by one, or copy a whole roster from another section (the
"previous year / sister section" case). These cover the unique-enrollment
constraint (a learner holds only one active enrollment per academic year), so a
learner picked from a sibling section is *moved*, never duplicated.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import StudentProfile

from .models import AcademicYear, Section, StudentEnrollment

User = get_user_model()
PASSWORD = "pass12345x"


class SectionStudentAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)
        group, _ = Group.objects.get_or_create(name="registrar")
        cls.registrar = User.objects.create_user(
            email="reg@tass.test", password=PASSWORD, status=User.Status.ACTIVE
        )
        cls.registrar.groups.add(group)

        cls.year = AcademicYear.objects.filter(status="ACTIVE").first()
        if cls.year is None:
            cls.year = AcademicYear.objects.create(
                label="2099-2100",
                start_date=date(2099, 6, 1),
                end_date=date(2100, 3, 31),
            )
        cls.other_year = AcademicYear.objects.create(
            label="2098-2099",
            start_date=date(2098, 6, 1),
            end_date=date(2099, 3, 31),
            status=AcademicYear.STATUS_ARCHIVED,
        )

        cls.section = Section.objects.create(
            academic_year=cls.year, name="Rose", grade_level=7
        )
        cls.sibling = Section.objects.create(
            academic_year=cls.year, name="NSTEM", grade_level=7
        )
        cls.last_year_section = Section.objects.create(
            academic_year=cls.other_year, name="Legacy", grade_level=7
        )

    def setUp(self):
        self.client.login(username=self.registrar.email, password=PASSWORD)

    def _student(self, lrn, last="Dela Cruz"):
        user = User.objects.create_user(
            email="{}@student.test".format(lrn), password=PASSWORD
        )
        return StudentProfile.objects.create(
            user=user,
            lrn=lrn,
            first_name="Juan",
            last_name=last,
            birthdate=date(2010, 1, 1),
        )

    def test_roster_page_reachable_and_lists_candidates(self):
        student = self._student("100000000001")
        resp = self.client.get(
            reverse("academic:section_students", args=[self.section.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Add Students One by One")
        self.assertContains(resp, "Bulk Assign from Another Section")
        self.assertIn(student, list(resp.context["candidates"]))

    def test_add_students_one_by_one(self):
        a = self._student("100000000002")
        b = self._student("100000000003")
        resp = self.client.post(
            reverse("academic:section_students", args=[self.section.pk]),
            {"students": [a.pk, b.pk]},
        )
        self.assertRedirects(resp, reverse("academic:section_students", args=[self.section.pk]))
        enrolled = StudentEnrollment.objects.filter(
            section=self.section, status=StudentEnrollment.STATUS_ENROLLED
        )
        self.assertEqual(enrolled.count(), 2)
        self.assertEqual(
            set(enrolled.values_list("student_id", flat=True)), {a.pk, b.pk}
        )

    def test_student_already_in_a_sibling_section_is_not_offered(self):
        """A placed learner is never silently pulled out of another class.

        The unique-per-year constraint means choosing them would move them, so
        they are kept off the add list; transferring a roster (or Promote
        Students) is the deliberate route for a move.
        """
        student = self._student("100000000004")
        StudentEnrollment.objects.create(
            student=student,
            academic_year=self.year,
            section=self.sibling,
            status=StudentEnrollment.STATUS_ENROLLED,
        )
        resp = self.client.get(
            reverse("academic:section_students", args=[self.section.pk])
        )
        self.assertNotIn(student, list(resp.context["candidates"]))

    def test_copy_roster_from_another_section(self):
        enrolled = [self._student("10000000000{}".format(i)) for i in (5, 6, 7)]
        for student in enrolled:
            StudentEnrollment.objects.create(
                student=student,
                academic_year=self.year,
                section=self.sibling,
                status=StudentEnrollment.STATUS_ENROLLED,
            )
        resp = self.client.post(
            reverse("academic:section_students", args=[self.section.pk]),
            {"copy_from_section": 1, "source_section": self.sibling.pk},
        )
        self.assertRedirects(resp, reverse("academic:section_students", args=[self.section.pk]))
        self.assertEqual(
            StudentEnrollment.objects.filter(
                section=self.section, status=StudentEnrollment.STATUS_ENROLLED
            ).count(),
            3,
        )
        # Copying a same-year roster transfers them: the sibling no longer lists
        # them, and each holds exactly one active enrollment this year.
        self.assertFalse(
            StudentEnrollment.objects.filter(
                section=self.sibling, status=StudentEnrollment.STATUS_ENROLLED
            ).exists()
        )
        for student in enrolled:
            self.assertEqual(
                StudentEnrollment.objects.filter(
                    student=student,
                    academic_year=self.year,
                    status=StudentEnrollment.STATUS_ENROLLED,
                ).count(),
                1,
            )

    def test_copy_from_empty_section_warns_and_changes_nothing(self):
        resp = self.client.post(
            reverse("academic:section_students", args=[self.section.pk]),
            {"copy_from_section": 1, "source_section": self.sibling.pk},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "has no enrolled students to copy")
        self.assertEqual(
            StudentEnrollment.objects.filter(
                section=self.section, status=StudentEnrollment.STATUS_ENROLLED
            ).count(),
            0,
        )

    def test_bulk_import_from_last_years_section_promotes_that_cohort(self):
        """The reported need: seed a new section from last year's section."""
        last_year_students = [self._student("10000000001{}".format(i)) for i in (1, 2)]
        for student in last_year_students:
            StudentEnrollment.objects.create(
                student=student,
                academic_year=self.other_year,
                section=self.last_year_section,
                status=StudentEnrollment.STATUS_ENROLLED,
            )
        # A same-named learner sitting in a section *this* year must not be
        # dragged in by the name collision; the source's own year is read.
        other_year = AcademicYear.objects.create(
            label="2097-2098",
            start_date=date(2097, 6, 1),
            end_date=date(2098, 3, 31),
            status=AcademicYear.STATUS_ARCHIVED,
        )
        decoy_section = Section.objects.create(
            academic_year=self.year, name="Decoy", grade_level=7
        )
        decoy = self._student("100000000013")
        StudentEnrollment.objects.create(
            student=decoy,
            academic_year=self.year,
            section=decoy_section,
            status=StudentEnrollment.STATUS_ENROLLED,
        )

        resp = self.client.post(
            reverse("academic:section_students", args=[self.section.pk]),
            {"copy_from_section": 1, "source_section": self.last_year_section.pk},
        )
        self.assertRedirects(
            resp, reverse("academic:section_students", args=[self.section.pk])
        )
        promoted = StudentEnrollment.objects.filter(
            section=self.section, status=StudentEnrollment.STATUS_ENROLLED
        )
        self.assertEqual(
            set(promoted.values_list("student_id", flat=True)),
            {s.pk for s in last_year_students},
        )
        # The promotion is a new row for the current year; last year's record
        # is untouched, and the decoy stayed in its own section.
        for student in last_year_students:
            self.assertTrue(
                StudentEnrollment.objects.filter(
                    student=student,
                    academic_year=self.other_year,
                    section=self.last_year_section,
                    status=StudentEnrollment.STATUS_ENROLLED,
                ).exists()
            )
        self.assertFalse(
            StudentEnrollment.objects.filter(
                section=self.section, student=decoy
            ).exists()
        )

    def test_previous_year_section_is_offered_as_a_copy_source(self):
        last_year_student = self._student("100000000008")
        StudentEnrollment.objects.create(
            student=last_year_student,
            academic_year=self.other_year,
            section=self.last_year_section,
            status=StudentEnrollment.STATUS_ENROLLED,
        )
        resp = self.client.get(
            reverse("academic:section_students", args=[self.section.pk])
        )
        offered = list(resp.context["move_form"].fields["source_section"].queryset)
        self.assertIn(self.last_year_section, offered)
        self.assertIn(self.sibling, offered)
        # The section itself is never a source of its own roster.
        self.assertNotIn(self.section, offered)

    def test_other_grade_levels_are_never_offered_as_a_source(self):
        """Last year's Grade 6 is not last year's Grade 7."""
        wrong_grade = Section.objects.create(
            academic_year=self.other_year, name="JuniorLegacy", grade_level=6
        )
        resp = self.client.get(
            reverse("academic:section_students", args=[self.section.pk])
        )
        offered = list(resp.context["move_form"].fields["source_section"].queryset)
        self.assertNotIn(wrong_grade, offered)

    def test_remove_student_from_roster_keeps_the_record(self):
        student = self._student("100000000009")
        enrollment = StudentEnrollment.objects.create(
            student=student,
            academic_year=self.year,
            section=self.section,
            status=StudentEnrollment.STATUS_ENROLLED,
        )
        resp = self.client.post(
            reverse(
                "academic:section_student_remove",
                args=[self.section.pk, enrollment.pk],
            )
        )
        self.assertRedirects(resp, reverse("academic:section_students", args=[self.section.pk]))
        enrollment.refresh_from_db()
        self.assertEqual(enrollment.status, StudentEnrollment.STATUS_ARCHIVED)
        self.assertTrue(StudentProfile.objects.filter(pk=student.pk).exists())

    def test_section_list_shows_roster_count_and_link(self):
        student = self._student("100000000010")
        StudentEnrollment.objects.create(
            student=student,
            academic_year=self.year,
            section=self.section,
            status=StudentEnrollment.STATUS_ENROLLED,
        )
        resp = self.client.get(reverse("academic:section_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["enrolled_counts"][self.section.pk], 1)
        self.assertContains(
            resp, reverse("academic:section_students", args=[self.section.pk])
        )


class SectionCreateRosterHandoffTests(TestCase):
    """"Save & Assign Students" must land on the roster of the new section."""

    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)
        group, _ = Group.objects.get_or_create(name="registrar")
        cls.registrar = User.objects.create_user(
            email="reg2@tass.test", password=PASSWORD, status=User.Status.ACTIVE
        )
        cls.registrar.groups.add(group)
        year = AcademicYear.objects.filter(status="ACTIVE").first()
        if year is None:
            year = AcademicYear.objects.create(
                label="2099-2100",
                start_date=date(2099, 6, 1),
                end_date=date(2100, 3, 31),
            )
        cls.year = year

    def setUp(self):
        self.client.login(username=self.registrar.email, password=PASSWORD)

    def _add_section(self, **extra):
        payload = {
            "academic_year": self.year.pk,
            "name": "Magnolia",
            "grade_level": 9,
            "status": Section.STATUS_ACTIVE,
        }
        payload.update(extra)
        return self.client.post(reverse("academic:section_add"), payload)

    def test_save_and_add_students_redirects_to_the_new_roster(self):
        resp = self._add_section(save_and_add_students="1")
        section = Section.objects.get(name="Magnolia")
        self.assertRedirects(resp, reverse("academic:section_students", args=[section.pk]))

    def test_plain_save_returns_to_the_list(self):
        resp = self._add_section()
        self.assertRedirects(resp, reverse("academic:section_list"))

    def test_section_form_offers_the_handoff_button(self):
        resp = self.client.get(reverse("academic:section_add"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Save &amp; Assign Students")
