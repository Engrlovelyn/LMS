"""The Assessment Repository must be scoped to the subjects a teacher teaches.

Two teachers assigned to the same subject see the same assessments (they are
co-teachers); a teacher assigned to another subject must not see them. The
scope comes from active VirtualClassrooms in the active academic year.
"""

import datetime
import json

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, RoleAssignment, User
from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.assessments.models import Assessment, AssessmentAssignment, AssessmentPart
from apps.assessments.views import _teacher_subject_ids
from apps.dashboards.context import teacher_context
from apps.virtual_classrooms.models import VirtualClassroom


class AssessmentRepositoryScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=datetime.date(2026, 8, 1),
            end_date=datetime.date(2027, 3, 31),
            status=AcademicYear.STATUS_ACTIVE,
        )
        cls.filipino = Subject.objects.create(
            academic_year=cls.year, name="Filipino 1", grade_level=7
        )
        cls.science = Subject.objects.create(
            academic_year=cls.year, name="Science 1", grade_level=7
        )
        role = Role.objects.get_or_create(code=Role.Code.TEACHER)[0]
        teacher_group = Group.objects.get_or_create(name="teacher")[0]

        def teacher(email):
            user = User.objects.create_user(email=email, password="TeachPass123!")
            user.status = User.Status.ACTIVE
            user.save()
            user.groups.add(teacher_group)
            RoleAssignment.objects.create(
                user=user, role=role, academic_year=cls.year
            )
            return user

        cls.teacher_a = teacher("filipino-a@test.local")
        cls.teacher_b = teacher("filipino-b@test.local")
        cls.science_teacher = teacher("science@test.local")

        for index, (teacher, subject) in enumerate((
            (cls.teacher_a, cls.filipino),
            (cls.teacher_b, cls.filipino),
            (cls.science_teacher, cls.science),
        )):
            section = Section.objects.create(
                academic_year=cls.year,
                name=f"Section {index + 1}",
                grade_level=7,
            )
            VirtualClassroom.objects.create(
                academic_year=cls.year,
                section=section,
                subject=subject,
                teacher=teacher,
            )

        deadline = timezone.now() + datetime.timedelta(days=7)
        cls.filipino_assessment = Assessment.objects.create(
            subject=cls.filipino,
            academic_year=cls.year,
            created_by=cls.teacher_a,
            title="Filipino Quiz",
            assessment_method=Assessment.METHOD_QUIZ,
            assessment_type=Assessment.TYPE_WRITTEN_ORAL,
            submission_deadline=deadline,
            share_to_repository=True,
        )
        cls.science_assessment = Assessment.objects.create(
            subject=cls.science,
            academic_year=cls.year,
            created_by=cls.science_teacher,
            title="Science Quiz",
            assessment_method=Assessment.METHOD_QUIZ,
            assessment_type=Assessment.TYPE_WRITTEN_ORAL,
            submission_deadline=deadline,
            share_to_repository=True,
        )

    def test_coteachers_resolve_to_the_same_subject_scope(self):
        self.assertEqual(
            _teacher_subject_ids(self.teacher_a),
            _teacher_subject_ids(self.teacher_b),
        )
        self.assertIn(self.filipino.id, _teacher_subject_ids(self.teacher_b))
        self.assertNotIn(self.science.id, _teacher_subject_ids(self.teacher_b))

    def test_teacher_context_repository_shares_with_coteacher(self):
        payload = teacher_context(self.teacher_b)
        titles = {
            row["title"]
            for row in payload["dashboard_json"]["assessment_repository"]
        }
        self.assertIn("Filipino Quiz", titles)
        self.assertNotIn("Science Quiz", titles)

    def test_unshared_assessment_stays_out_of_coteacher_repository(self):
        private = Assessment.objects.create(
            subject=self.filipino,
            academic_year=self.year,
            created_by=self.teacher_a,
            title="Private Filipino Quiz",
            assessment_method=Assessment.METHOD_QUIZ,
            assessment_type=Assessment.TYPE_WRITTEN_ORAL,
            submission_deadline=timezone.now() + datetime.timedelta(days=7),
        )
        self.client.force_login(self.teacher_b)
        response = self.client.get(reverse("assessments:assessment_repository"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, private.title)
        self.client.force_login(self.teacher_a)
        response = self.client.get(reverse("assessments:assessment_repository"))
        self.assertNotContains(response, private.title)

    def test_repository_page_hides_other_subjects(self):
        self.client.force_login(self.teacher_b)
        response = self.client.get(reverse("assessments:assessment_repository"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Filipino Quiz", content)
        self.assertNotIn("Science Quiz", content)

    def test_science_teacher_cannot_see_filipino(self):
        self.client.force_login(self.science_teacher)
        response = self.client.get(reverse("assessments:assessment_repository"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Science Quiz", content)
        self.assertNotIn("Filipino Quiz", content)

    def test_profile_reports_subjects_taught(self):
        payload = teacher_context(self.teacher_b)
        self.assertEqual(payload["dashboard_json"]["profile"]["subjects"], ["Filipino 1"])
        repository_subjects = payload["dashboard_json"]["assessment_repository_subjects"]
        self.assertEqual([subject["name"] for subject in repository_subjects], ["Filipino 1"])

    def test_teacher_group_without_role_assignment_can_create_assessment(self):
        teacher_group = Group.objects.get(name="teacher")
        teacher_group.permissions.add(
            Permission.objects.get(
                content_type__app_label="assessments",
                codename="view_assessment_custom",
            ),
            Permission.objects.get(
                content_type__app_label="assessments",
                codename="create_assessment_custom",
            ),
        )
        teacher = User.objects.create_user(
            email="group-only-teacher@test.local",
            password="TeachPass123!",
        )
        teacher.status = User.Status.ACTIVE
        teacher.save()
        teacher.groups.add(teacher_group)
        section = Section.objects.create(
            academic_year=self.year,
            name="Group-only teacher section",
            grade_level=7,
        )
        VirtualClassroom.objects.create(
            academic_year=self.year,
            section=section,
            subject=self.filipino,
            teacher=teacher,
        )
        self.client.force_login(teacher)

        listing = self.client.get(reverse("assessments:assessment_list"))
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, reverse("assessments:assessment_add"))

        creation_form = self.client.get(reverse("assessments:assessment_add"))
        self.assertEqual(creation_form.status_code, 200)

    def test_teacher_can_reuse_archived_assessment_in_matching_current_class(self):
        old_year = AcademicYear.objects.create(
            label="2025-2026",
            start_date=datetime.date(2025, 8, 1),
            end_date=datetime.date(2026, 3, 31),
            status=AcademicYear.STATUS_ARCHIVED,
        )
        old_subject = Subject.objects.create(
            academic_year=old_year, name="Filipino 1", grade_level=7
        )
        archived = Assessment.objects.create(
            subject=old_subject,
            academic_year=old_year,
            created_by=self.teacher_a,
            title="Archived Filipino Quiz",
            assessment_method=Assessment.METHOD_QUIZ,
            assessment_type=Assessment.TYPE_WRITTEN_ORAL,
            submission_deadline=timezone.now() + datetime.timedelta(days=7),
            share_to_repository=True,
            status=Assessment.STATUS_ARCHIVED,
        )
        AssessmentPart.objects.create(
            assessment=archived,
            question_type=AssessmentPart.TYPE_MULTIPLE_CHOICE,
            items={"item_1": {"question": "Q?", "point": 3}},
        )
        target = VirtualClassroom.objects.get(
            teacher=self.teacher_a, subject=self.filipino
        )
        self.client.force_login(self.teacher_a)

        response = self.client.post(
            reverse("api_assessment_repository_copy"),
            {
                "assessment_id": archived.pk,
                "virtual_classroom_ids": json.dumps([target.pk]),
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["copied_classrooms"], 1)
        clone = Assessment.objects.get(pk=payload["assessment_ids"][0])
        self.assertEqual(clone.academic_year, self.year)
        self.assertEqual(clone.subject, self.filipino)
        self.assertFalse(clone.share_to_repository)
        self.assertTrue(
            AssessmentAssignment.objects.filter(
                assessment=clone, virtual_classroom=target
            ).exists()
        )
        self.assertEqual(clone.parts.get().points, 3)
