"""Assessment integrity: the proctoring-event endpoint.

These prove the security signal is recorded only for a student who is actually
enrolled and assigned the assessment, that a burst of duplicate events collapses
into one row, and that non-students / non-enrolled users are refused.
"""

import datetime
import json
from decimal import Decimal

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StudentProfile, User
from apps.communication.models import Notification
from apps.academic_structure.models import (
    AcademicStructureVersion,
    AcademicYear,
    Section,
    Subject,
)
from apps.assessments.models import (
    Assessment,
    AssessmentAssignment,
    AssessmentAttempt,
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
        cls.teacher.groups.add(Group.objects.get_or_create(name="teacher")[0])
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

    def test_screenshot_event_reaches_teacher_activity_log_and_bell(self):
        self.client.login(username=self.student.email, password="pw")
        response = self._post("screenshot_key")
        self.assertEqual(response.status_code, 200, response.content.decode())
        event = AssessmentProctoringEvent.objects.get(
            assessment=self.assessment,
            student=self.profile,
            event_type="screenshot_key",
        )
        notification = Notification.objects.get(
            recipient=self.teacher,
            notification_type=Notification.TYPE_ASSESSMENT,
            source_type="assessment_proctoring_event",
            source_id=event.pk,
        )

        from apps.communication.notifications import can_access, notification_target
        from apps.dashboards.context import teacher_context

        self.assertTrue(can_access(notification, self.teacher))
        target = notification_target(notification, user=self.teacher)
        self.assertIn("notification_tab=activitylog", target)
        self.assertIn("notification_record_type=proctoring", target)
        self.assertIn("notification_record={}".format(event.pk), target)

        activity = teacher_context(self.teacher)["dashboard_json"]["activity"]
        logged = next(row for row in activity if row.get("recordId") == event.pk)
        self.assertEqual(logged["recordType"], "proctoring")
        self.assertIn(self.profile.last_name, logged["text"])

        self.client.force_login(self.teacher)
        opened = self.client.get(
            reverse("communication:notification_open", args=[notification.pk])
        )
        self.assertEqual(opened.status_code, 302)
        self.assertIn("notification_tab=activitylog", opened["Location"])
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

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

    def test_tab_switch_event_notifies_the_classroom_teacher(self):
        self.client.login(username=self.student.email, password="pw")
        response = self._post("visibility_hidden")
        self.assertEqual(response.status_code, 200, response.content.decode())
        event = AssessmentProctoringEvent.objects.get(
            assessment=self.assessment,
            student=self.profile,
            event_type="visibility_hidden",
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.teacher,
                source_type="assessment_proctoring_event",
                source_id=event.pk,
            ).exists()
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


class AssessmentAttemptAutosaveTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Permission

        cls.year = AcademicYear.objects.create(
            label="2026 AT", start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31), status="ACTIVE",
        )
        cls.section = Section.objects.create(academic_year=cls.year, name="AT - A", grade_level=9)
        cls.subject = Subject.objects.create(academic_year=cls.year, name="Math", grade_level=9)
        cls.teacher = User.objects.create_user(email="at_teacher@tass.local", password="pw")
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year, section=cls.section, subject=cls.subject, teacher=cls.teacher
        )
        cls.student, cls.profile = _student_user("at_student", cls.year, 31)
        cls.outsider, _ = _student_user("at_outsider", cls.year, 32)
        for user in (cls.student, cls.outsider, cls.teacher):
            user.status = User.Status.ACTIVE
            user.save(update_fields=["status", "is_active"])
        VirtualClassroomEnrollment.objects.create(virtual_classroom=cls.vc, student=cls.profile)
        cls.assessment = Assessment.objects.create(
            subject=cls.subject, academic_year=cls.year, created_by=cls.teacher,
            title="Timed Quiz", assessment_method=Assessment.METHOD_QUIZ,
            assessment_type=Assessment.TYPE_WRITTEN_ORAL,
            status=Assessment.STATUS_ACTIVE,
            available_at=timezone.now() - datetime.timedelta(minutes=1),
            submission_deadline=timezone.now() + datetime.timedelta(days=1),
        )
        cls.assignment = AssessmentAssignment.objects.create(
            assessment=cls.assessment, virtual_classroom=cls.vc, assigned_by=cls.teacher
        )
        cls.part = AssessmentPart.objects.create(
            assessment=cls.assessment,
            question_type=AssessmentPart.TYPE_MULTIPLE_CHOICE,
            order=1,
            items={"item_1": {"question": "Pick A", "choices": ["A", "B"], "correct_answer": "A", "point": 1}},
        )
        cls.student.user_permissions.add(
            Permission.objects.get(codename="complete_assessment_custom")
        )
        cls.teacher.user_permissions.add(
            Permission.objects.get(codename="create_assessment_custom")
        )

    def _open(self):
        self.client.force_login(self.student)
        return self.client.get(
            reverse("assessments:assessment_submit", args=[self.assessment.pk]),
            {"assignment_id": self.assignment.pk},
        )

    def _autosave(self, attempt, revision, answer, user=None):
        if user:
            self.client.force_login(user)
        return self.client.post(
            reverse("api_submission_autosave"),
            data=json.dumps({
                "assessment_id": self.assessment.pk,
                "assignment_id": self.assignment.pk,
                "attempt_id": attempt.pk,
                "revision": revision,
                "answers": {"part_{}__item_1".format(self.part.pk): answer},
            }),
            content_type="application/json",
        )

    def test_builder_images_are_saved_and_returned_to_the_student_assessment(self):
        image = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        )
        payload = {
            "virtual_classroom_id": self.vc.pk,
            "title": "Image Quiz",
            "type": "Quiz",
            "openDate": (timezone.now() - datetime.timedelta(minutes=1)).isoformat(),
            "deadline": (timezone.now() + datetime.timedelta(days=1)).isoformat(),
            "questions": [{
                "type": "mc",
                "text": "Which picture matches the question picture?",
                "points": 1,
                "media": {"kind": "image", "name": "question.png", "data": image},
                "options": ["", "Choice B"],
                "optImgs": {"A": image, "B": image},
                "correct": 0,
            }],
        }
        self.client.force_login(self.teacher)
        saved = self.client.post(
            reverse("api_assessments"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(saved.status_code, 200, saved.content.decode())
        self.assertTrue(saved.json()["ok"])

        assessment = Assessment.objects.get(pk=saved.json()["assessment_id"])
        assignment = AssessmentAssignment.objects.get(assessment=assessment, virtual_classroom=self.vc)
        part = assessment.parts.get()
        item = part.items["item_1"]
        self.assertEqual(item["image"], image)
        self.assertEqual(item["choice_images"], [image, image])
        self.assertEqual(item["choices"], ["Choice A", "Choice B"])

        self.client.force_login(self.student)
        opened = self.client.get(
            reverse("assessments:assessment_submit", args=[assessment.pk]),
            {"assignment_id": assignment.pk, "format": "json"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(opened.status_code, 200, opened.content.decode())
        question = opened.json()["parts"][0]["questions"][0]
        self.assertEqual(question["image"], image)
        self.assertEqual(question["choice_images"], [image, image])

    def test_future_assessment_direct_url_is_rejected_without_starting_attempt(self):
        self.assessment.available_at = timezone.now() + datetime.timedelta(hours=1)
        self.assessment.save(update_fields=["available_at"])
        response = self._open()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AssessmentAttempt.objects.exists())

    def test_autosave_is_versioned_and_does_not_create_a_submission(self):
        response = self._open()
        self.assertEqual(response.status_code, 200, response.content.decode())
        attempt = AssessmentAttempt.objects.get(assignment=self.assignment, student=self.profile)
        self.assertGreater(attempt.expires_at, attempt.started_at)

        newer = self._autosave(attempt, 2, "B")
        older = self._autosave(attempt, 1, "A")
        self.assertEqual(newer.status_code, 200)
        self.assertEqual(older.status_code, 200)
        attempt.refresh_from_db()
        self.assertEqual(attempt.revision, 2)
        self.assertEqual(attempt.answers["part_{}__item_1".format(self.part.pk)], "B")
        self.assertFalse(Submission.objects.filter(assessment_assignment=self.assignment).exists())

    def test_refresh_resumes_the_same_clock_instead_of_starting_a_new_attempt(self):
        first = self._open()
        self.assertEqual(first.status_code, 200)
        attempt = AssessmentAttempt.objects.get(assignment=self.assignment, student=self.profile)
        attempt.expires_at = timezone.now() - datetime.timedelta(seconds=1)
        expired_at = attempt.expires_at
        attempt.save(update_fields=["expires_at"])

        refreshed = self._open()
        attempt.refresh_from_db()
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(AssessmentAttempt.objects.filter(assignment=self.assignment, student=self.profile).count(), 1)
        self.assertEqual(attempt.expires_at, expired_at)
        self.assertEqual(refreshed.context["remaining_seconds"], 0)

    def test_autosave_rejects_another_students_attempt_and_unknown_question(self):
        self._open()
        attempt = AssessmentAttempt.objects.get(assignment=self.assignment, student=self.profile)
        denied = self._autosave(attempt, 1, "A", user=self.outsider)
        self.assertEqual(denied.status_code, 404)

        self.client.force_login(self.student)
        response = self.client.post(
            reverse("api_submission_autosave"),
            data=json.dumps({
                "assessment_id": self.assessment.pk,
                "assignment_id": self.assignment.pk,
                "attempt_id": attempt.pk,
                "revision": 1,
                "answers": {"part_999__not_a_question": "A"},
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

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
