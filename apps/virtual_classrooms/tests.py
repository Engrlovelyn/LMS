import json
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse

from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.dashboards.context import assistant_principal_context, registrar_context
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
            status=AcademicYear.STATUS_ACTIVE,
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

    def test_classroom_form_exposes_grade_and_year_for_dependent_subject_filtering(self):
        form = VirtualClassroomForm()
        self.assertEqual(form.fields["section"].queryset.count(), 1)
        self.assertEqual(form.fields["subject"].queryset.count(), 1)
        section_html = str(form["section"])
        subject_html = str(form["subject"])
        self.assertIn('data-grade="11"', section_html)
        self.assertIn('data-year="{}"'.format(self.year.pk), section_html)
        self.assertIn('data-grade="11"', subject_html)
        self.assertIn('data-year="{}"'.format(self.year.pk), subject_html)


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
        self.teacher = User.objects.create_user(
            email="teacher@test.ph", password="pass", status=User.Status.ACTIVE
        )
        self.other_teacher = User.objects.create_user(
            email="teacher2@test.ph", password="pass", status=User.Status.ACTIVE
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
        # `taken` is the classroom being edited, so its own teacher stays
        # eligible; the teacher occupied by the other classroom must not be
        # picked.
        self.assertNotEqual(third, self.teacher)
        self.assertEqual(third, self.other_teacher)


class CreateVirtualClassroomApiTests(TestCase):
    """The single-classroom endpoint must validate, authorize, persist, and be
    visible on the dashboards that render it."""

    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)

    def setUp(self):
        self.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 3, 31),
            status=AcademicYear.STATUS_ACTIVE,
        )
        self.section = Section.objects.create(
            academic_year=self.year, name="STEM-Newton", grade_level=11
        )
        self.other_grade_section = Section.objects.create(
            academic_year=self.year, name="STEM-Galileo", grade_level=10
        )
        self.subject = Subject.objects.create(
            academic_year=self.year, name="Science 11", grade_level=11
        )
        self.other_grade_subject = Subject.objects.create(
            academic_year=self.year, name="Science 10", grade_level=10
        )
        self.teacher = User.objects.create_user(
            email="teacher@test.ph",
            password="pass12345x",
            status=User.Status.ACTIVE,
        )
        self.teacher.groups.add(Group.objects.get_or_create(name="teacher")[0])
        self.registrar = User.objects.create_user(
            email="registrar@test.ph",
            password="pass12345x",
            status=User.Status.ACTIVE,
        )
        self.registrar.groups.add(Group.objects.get_or_create(name="registrar")[0])

    def _post(self, user, **overrides):
        self.client.force_login(user)
        payload = {
            "academic_year": self.year.label,
            "section": self.section.name,
            "subject": self.subject.name,
            "grade": self.section.grade_level,
            "teacher_id": self.teacher.pk,
        }
        payload.update(overrides)
        return self.client.post(
            reverse("api_virtual_classroom_create"),
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_create_persists_classroom(self):
        resp = self._post(self.registrar)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["created"])
        vc = VirtualClassroom.objects.get()
        self.assertEqual(vc.subject, self.subject)
        self.assertEqual(vc.section, self.section)
        self.assertEqual(vc.teacher, self.teacher)

    def test_create_is_idempotent_for_same_key(self):
        self.assertEqual(self._post(self.registrar).status_code, 200)
        second = self._post(self.registrar)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(VirtualClassroom.objects.count(), 1)

    def test_repost_without_teacher_keeps_existing_teacher(self):
        self._post(self.registrar)
        vc = VirtualClassroom.objects.get()
        resp = self._post(self.registrar, teacher_id="")
        self.assertEqual(resp.status_code, 200, resp.content)
        vc.refresh_from_db()
        self.assertEqual(vc.teacher, self.teacher)

    def test_create_requires_permission(self):
        outsider = User.objects.create_user(
            email="student@test.ph",
            password="pass12345x",
            status=User.Status.ACTIVE,
        )
        outsider.groups.add(Group.objects.get_or_create(name="student")[0])
        resp = self._post(outsider)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(VirtualClassroom.objects.count(), 0)

    def test_authorized_teacher_can_create(self):
        from django.contrib.auth.models import Permission

        self.teacher.user_permissions.add(
            Permission.objects.get(codename="create_virtual_classroom")
        )
        resp = self._post(self.teacher)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(VirtualClassroom.objects.filter(teacher=self.teacher).exists())

    def test_missing_section_is_rejected(self):
        resp = self._post(self.registrar, section="")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("section", resp.json()["message"].lower())

    def test_missing_subject_is_rejected(self):
        resp = self._post(self.registrar, subject="")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("subject", resp.json()["message"].lower())

    def test_grade_mismatch_is_rejected(self):
        resp = self._post(
            self.registrar,
            section_id=self.other_grade_section.pk,
            subject_id=self.subject.pk,
            grade=self.other_grade_section.grade_level,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("grade", resp.json()["message"].lower())
        self.assertEqual(VirtualClassroom.objects.count(), 0)

    def test_grade_label_string_still_resolves(self):
        resp = self._post(self.registrar, grade="Grade {}".format(self.section.grade_level))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(VirtualClassroom.objects.count(), 1)

    def test_teacher_clash_is_rejected(self):
        second_teacher = User.objects.create_user(
            email="teacher2@test.ph",
            password="pass12345x",
            status=User.Status.ACTIVE,
        )
        second_teacher.groups.add(Group.objects.get_or_create(name="teacher")[0])
        self._post(self.registrar, teacher=self.teacher.email)
        second_subject = Subject.objects.create(
            academic_year=self.year, name="Math 11", grade_level=11
        )
        resp = self._post(
            self.registrar,
            subject=second_subject.name,
            teacher=self.teacher.email,
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("already teaches", resp.json()["message"])

    def test_bridge_generic_period_does_not_duplicate(self):
        self.client.force_login(self.registrar)

        def post(period):
            return self.client.post(
                reverse("api_academic_structure"),
                data=json.dumps(
                    {
                        "sections": [],
                        "subjects": [],
                        "vcs": [
                            {
                                "subject": self.subject.name,
                                "section": self.section.name,
                                "grade": self.section.grade_level,
                                "gradingPeriod": period,
                            }
                        ],
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(post("Quarter").status_code, 200)
        self.assertEqual(post("").status_code, 200)
        self.assertEqual(VirtualClassroom.objects.count(), 1)

    def test_created_classroom_appears_on_registrar_dashboard(self):
        self._post(self.registrar)
        context = registrar_context(self.registrar)
        names = [vc["subject"] for vc in context["dashboard_json"]["virtualClassrooms"]]
        self.assertIn(self.subject.name, names)

    def test_created_classroom_appears_on_coordinator_dashboard(self):
        self._post(self.registrar)
        coordinator = User.objects.create_user(
            email="coordinator@test.ph",
            password="pass12345x",
            status=User.Status.ACTIVE,
        )
        coordinator.groups.add(
            Group.objects.get_or_create(name="assistant_principal")[0]
        )
        context = assistant_principal_context(coordinator)
        names = [vc["subject"] for vc in context["dashboard_json"]["vcList"]]
        self.assertIn(self.subject.name, names)


class ClassroomAddViewPermissionTests(TestCase):
    """`classroom_add` used to be limited to registrar/assistant_principal by a
    role decorator, which blocked an authorized teacher. It now keys off the
    permission only."""

    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)

    def setUp(self):
        self.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 3, 31),
            status=AcademicYear.STATUS_ACTIVE,
        )
        self.section = Section.objects.create(
            academic_year=self.year, name="STEM-Newton", grade_level=11
        )
        self.subject = Subject.objects.create(
            academic_year=self.year, name="Science 11", grade_level=11
        )
        self.teacher = User.objects.create_user(
            email="teacher@test.ph",
            password="pass12345x",
            status=User.Status.ACTIVE,
        )
        self.teacher.groups.add(Group.objects.get_or_create(name="teacher")[0])

    def _payload(self, **overrides):
        payload = {
            "academic_year": self.year.pk,
            "section": self.section.pk,
            "subject": self.subject.pk,
            "teacher": self.teacher.pk,
            "grading_period": "",
            "schedule": "",
            "theme_color": "#4F46E5",
            "status": VirtualClassroom.STATUS_ACTIVE,
        }
        payload.update(overrides)
        return payload

    def test_authorized_teacher_can_open_and_submit(self):
        from django.contrib.auth.models import Permission

        self.teacher.user_permissions.add(
            Permission.objects.get(codename="create_virtual_classroom")
        )
        self.client.force_login(self.teacher)
        self.assertEqual(
            self.client.get(reverse("classrooms:classroom_add")).status_code, 200
        )
        resp = self.client.post(
            reverse("classrooms:classroom_add"), data=self._payload()
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(VirtualClassroom.objects.filter(teacher=self.teacher).exists())

    def test_unauthorized_teacher_is_blocked(self):
        self.client.force_login(self.teacher)
        self.assertEqual(
            self.client.get(reverse("classrooms:classroom_add")).status_code, 403
        )


class ClassroomListSearchTests(TestCase):
    """The server-rendered Virtual Classrooms page must let a user search by
    subject, section, academic year or teacher and filter the rows."""

    @classmethod
    def setUpTestData(cls):
        from apps.accounts.models import EmployeeProfile

        call_command("assign_role_permissions", verbosity=0)
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 3, 31),
            status=AcademicYear.STATUS_ACTIVE,
        )
        cls.math_section = Section.objects.create(
            academic_year=cls.year, name="Bonifacio", grade_level=9
        )
        cls.science_section = Section.objects.create(
            academic_year=cls.year, name="Mabini", grade_level=10
        )
        cls.math = Subject.objects.create(
            academic_year=cls.year, name="Mathematics", grade_level=9
        )
        cls.science = Subject.objects.create(
            academic_year=cls.year,
            name="Science",
            grade_level=10,
            subject_type=Subject.TYPE_ELECTIVE,
        )
        cls.math_teacher = User.objects.create_user(
            email="reyes@test.ph", password="pass12345x", status=User.Status.ACTIVE
        )
        EmployeeProfile.objects.create(
            user=cls.math_teacher,
            employee_number="EMP-REYES",
            first_name="Lorenzo",
            last_name="Reyes",
            sex="M",
            birthdate=date(1988, 2, 11),
        )
        cls.science_teacher = User.objects.create_user(
            email="cruz@test.ph", password="pass12345x", status=User.Status.ACTIVE
        )
        EmployeeProfile.objects.create(
            user=cls.science_teacher,
            employee_number="EMP-CRUZ",
            first_name="Maria",
            last_name="Cruz",
            sex="F",
            birthdate=date(1990, 5, 20),
        )
        for teacher in (cls.math_teacher, cls.science_teacher):
            teacher.groups.add(Group.objects.get_or_create(name="teacher")[0])
        VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.math_section,
            subject=cls.math,
            teacher=cls.math_teacher,
        )
        VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.science_section,
            subject=cls.science,
            teacher=cls.science_teacher,
        )
        cls.registrar = User.objects.create_user(
            email="registrar@test.ph",
            password="pass12345x",
            status=User.Status.ACTIVE,
        )
        cls.registrar.groups.add(Group.objects.get_or_create(name="registrar")[0])

    def setUp(self):
        self.client.force_login(self.registrar)

    def _subjects(self, q=""):
        resp = self.client.get(reverse("classrooms:classroom_list"), {"q": q})
        self.assertEqual(resp.status_code, 200)
        return {c.subject.name for c in resp.context["classrooms"]}

    def _filtered_subjects(self, **filters):
        resp = self.client.get(reverse("classrooms:classroom_list"), filters)
        self.assertEqual(resp.status_code, 200)
        return {c.subject.name for c in resp.context["classrooms"]}

    def test_page_has_search_bar(self):
        resp = self.client.get(reverse("classrooms:classroom_list"))
        self.assertContains(resp, 'id="vcSearch"')
        self.assertContains(resp, 'name="q"')
        self.assertContains(resp, 'name="subject_type"')
        self.assertContains(resp, 'name="grade_level"')

    def test_search_by_subject(self):
        self.assertEqual(self._subjects("Mathematics"), {"Mathematics"})

    def test_search_by_section(self):
        self.assertEqual(self._subjects("Mabini"), {"Science"})

    def test_search_by_teacher(self):
        self.assertEqual(self._subjects("Reyes"), {"Mathematics"})

    def test_blank_search_returns_everything(self):
        self.assertEqual(self._subjects(""), {"Mathematics", "Science"})

    def test_filter_by_subject_type(self):
        self.assertEqual(
            self._filtered_subjects(subject_type=Subject.TYPE_ELECTIVE),
            {"Science"},
        )

    def test_filter_by_grade_level(self):
        self.assertEqual(self._filtered_subjects(grade_level="9"), {"Mathematics"})
