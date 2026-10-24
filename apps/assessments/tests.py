"""Assessment integrity: the proctoring-event endpoint.

These prove the security signal is recorded only for a student who is actually
enrolled and assigned the assessment, that a burst of duplicate events collapses
into one row, and that non-students / non-enrolled users are refused.
"""

import datetime
import json
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StudentProfile, User
from apps.academic_structure.models import (
    AcademicStructureVersion,
    AcademicYear,
    Section,
    Subject,
)
from apps.assessments.models import (
    Assessment,
    AssessmentAssignment,
    AssessmentPart,
    AssessmentProctoringEvent,
    GradingComponent,
    GradingFormula,
    StudentGrade,
    Submission,
    SubmissionResponse,
)
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment


def _student_user(tag, year, n):
    user = User.objects.create_user(email=f"{tag}@tass.local", password="pw")
    profile = StudentProfile.objects.create(
        user=user,
        lrn=f"LRN-{year.label}-{n:04d}",
        first_name=tag.title(),
        last_name="Student",
        birthdate=datetime.date(2009, 1, 1),
        sex="M",
    )
    return user, profile


class ProctoringEventTests(TestCase):
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
        cls.teacher = User.objects.create_user(email="pa_teacher@tass.local", password="pw")
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section,
            subject=cls.subject,
            teacher=cls.teacher,
        )
        cls.student, cls.profile = _student_user("pa_alice", cls.year, 21)
        cls.outsider, cls.profile_out = _student_user("pa_cara", cls.year, 22)
        for u in (cls.student, cls.outsider, cls.teacher):
            u.status = "active"
            u.save(update_fields=["status", "is_active"])
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=cls.vc, student=cls.profile
        )
        cls.assessment = Assessment.objects.create(
            subject=cls.subject,
            academic_year=cls.year,
            created_by=cls.teacher,
            title="Midterm",
            assessment_method=Assessment.METHOD_EXAMINATION,
            assessment_type=Assessment.TYPE_SUMMATIVE_TEST,
            status=Assessment.STATUS_ACTIVE,
            submission_deadline=timezone.now() + datetime.timedelta(days=3),
        )
        AssessmentAssignment.objects.create(
            assessment=cls.assessment, virtual_classroom=cls.vc, assigned_by=cls.teacher
        )

    def _post(self, event_type):
        return self.client.post(
            reverse("assessments:record_proctoring_event", args=[self.assessment.pk]),
            data=json.dumps({"event_type": event_type}),
            content_type="application/json",
        )

    def test_enrolled_student_can_record_event(self):
        self.client.login(username=self.student.email, password="pw")
        resp = self._post("focus_lost")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["recorded"])
        self.assertEqual(
            AssessmentProctoringEvent.objects.filter(assessment=self.assessment).count(), 1
        )

    def test_duplicate_events_within_cooldown_collapse(self):
        self.client.login(username=self.student.email, password="pw")
        self._post("visibility_hidden")
        self._post("visibility_hidden")
        self.assertEqual(
            AssessmentProctoringEvent.objects.filter(
                assessment=self.assessment, event_type="visibility_hidden"
            ).count(),
            1,
        )

    def test_non_enrolled_student_is_refused(self):
        self.client.login(username=self.outsider.email, password="pw")
        resp = self._post("focus_lost")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(AssessmentProctoringEvent.objects.count(), 0)

    def test_teacher_cannot_record(self):
        self.client.login(username=self.teacher.email, password="pw")
        resp = self._post("focus_lost")
        self.assertEqual(resp.status_code, 403)

    def test_unknown_event_type_is_rejected(self):
        self.client.login(username=self.student.email, password="pw")
        resp = self._post("not_a_real_event")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(AssessmentProctoringEvent.objects.count(), 0)


class _GradedAssessmentFixture(TestCase):
    """Shared fixture: a classroom with one objectively-graded submission."""

    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026 PA",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            status="ACTIVE",
        )
        cls.structure = AcademicStructureVersion.objects.create(
            academic_year=cls.year,
            grading_period_type=AcademicStructureVersion.QUARTER,
        )
        cls.section = Section.objects.create(
            academic_year=cls.year, name="PA - A", grade_level=9
        )
        cls.subject = Subject.objects.create(
            academic_year=cls.year, name="Math", grade_level=9
        )
        cls.admin = User.objects.create_superuser(email="ag_admin@tass.local", password="pw")
        cls.admin.status = User.Status.ACTIVE
        cls.admin.save()
        cls.teacher = User.objects.create_user(email="ag_teacher@tass.local", password="pw")
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section,
            subject=cls.subject,
            teacher=cls.teacher,
        )
        student_user = User.objects.create_user(email="ag_student@tass.local", password="pw")
        cls.student = StudentProfile.objects.create(
            user=student_user,
            lrn="LRN-2026-9001",
            first_name="Liza",
            last_name="Cruz",
            birthdate=datetime.date(2009, 1, 1),
            sex="F",
        )
        VirtualClassroomEnrollment.objects.create(virtual_classroom=cls.vc, student=cls.student)

        cls.assessment = Assessment.objects.create(
            subject=cls.subject,
            academic_year=cls.year,
            created_by=cls.teacher,
            title="Quiz 1",
            assessment_method=Assessment.METHOD_EXAMINATION,
            assessment_type=Assessment.TYPE_SUMMATIVE_TEST,
            status=Assessment.STATUS_ACTIVE,
            submission_deadline=timezone.now() + datetime.timedelta(days=3),
        )
        cls.assignment = AssessmentAssignment.objects.create(
            assessment=cls.assessment, virtual_classroom=cls.vc, assigned_by=cls.teacher
        )
        cls.part = AssessmentPart.objects.create(
            assessment=cls.assessment,
            question_type=AssessmentPart.TYPE_IDENTIFICATION,
            order=1,
            items={
                "item_1": {
                    "question": "Capital of France?",
                    "accepted_answers": ["Paris"],
                    "point": 5,
                }
            },
        )
        cls.submission = Submission.objects.create(
            assessment_assignment=cls.assignment, student=cls.student
        )
        cls.response = SubmissionResponse.objects.create(
            submission=cls.submission, part=cls.part, response_data={"item_1": "Paris"}
        )

    def setUp(self):
        self.client.force_login(self.admin)


class AutoGradeSubmissionTests(_GradedAssessmentFixture):
    def test_auto_grade_scores_objective_response(self):
        res = self.client.post(
            reverse(
                "assessments:auto_grade_submission",
                args=[self.assessment.pk, self.submission.pk],
            )
        )
        self.assertEqual(res.status_code, 302)
        self.response.refresh_from_db()
        self.assertEqual(self.response.score, Decimal("5"))
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.total_score, Decimal("5"))
        self.assertEqual(self.submission.grading_status, Submission.GRADING_COMPLETE)

    def test_wrong_answer_scores_zero(self):
        self.response.response_data = {"item_1": "London"}
        self.response.save()
        self.client.post(
            reverse(
                "assessments:auto_grade_submission",
                args=[self.assessment.pk, self.submission.pk],
            )
        )
        self.response.refresh_from_db()
        self.assertEqual(self.response.score, Decimal("0"))


class ComputeGradesTests(_GradedAssessmentFixture):
    def test_compute_grades_creates_student_grade(self):
        formula = GradingFormula.objects.create(
            virtual_classroom=self.vc, structure_version=self.structure
        )
        GradingComponent.objects.create(
            formula=formula,
            component_type=Assessment.TYPE_SUMMATIVE_TEST,
            weight_percentage=100,
        )
        self.submission.total_score = Decimal("5")
        self.submission.grading_status = Submission.GRADING_COMPLETE
        self.submission.save()

        res = self.client.post(
            reverse("assessments:compute_grades", args=[self.vc.pk])
        )
        self.assertEqual(res.status_code, 302)
        grade = StudentGrade.objects.get(student=self.student, subject=self.subject)
        self.assertEqual(grade.grade_value, Decimal("100.00"))
