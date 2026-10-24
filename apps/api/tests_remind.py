import datetime
import json

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.assessments.models import Assessment, AssessmentAssignment
from apps.communication.models import Notification
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment


def _student(tag, year, n):
    user = User.objects.create_user(email=f"{tag}@tass.local", password="pw")
    user.status = User.Status.ACTIVE
    user.save()
    profile = StudentProfile.objects.create(
        user=user,
        lrn=f"LRN-{year.label}-{n:04d}",
        first_name=tag.title(),
        last_name="Student",
        birthdate=datetime.date(2009, 1, 1),
        sex="M",
    )
    return user, profile


class RemindTests(TestCase):
    def setUp(self):
        self.year = AcademicYear.objects.create(
            label="2026 GA",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            status="ACTIVE",
        )
        self.section = Section.objects.create(
            academic_year=self.year, name="JHS - A", grade_level=7
        )
        self.subject = Subject.objects.create(
            academic_year=self.year, name="Mathematics", grade_level=7
        )
        self.teacher = User.objects.create_user(email="t@tass.local", password="pw")
        self.teacher.status = User.Status.ACTIVE
        self.teacher.save()
        self.vc = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
        )
        self.student, self.profile = _student("s_alice", self.year, 1)
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=self.vc, student=self.profile
        )
        self.assessment = Assessment.objects.create(
            subject=self.subject,
            academic_year=self.year,
            created_by=self.teacher,
            title="Quiz 1",
            assessment_method=Assessment.METHOD_QUIZ,
            assessment_type=Assessment.TYPE_WRITTEN_ORAL,
            status=Assessment.STATUS_ACTIVE,
            submission_deadline=timezone.now() + datetime.timedelta(days=3),
        )
        self.assignment = AssessmentAssignment.objects.create(
            assessment=self.assessment, virtual_classroom=self.vc, assigned_by=self.teacher
        )
        self.client.force_login(self.teacher)
        self.url = reverse("api_remind")

    def post(self, body):
        return self.client.post(
            self.url, data=json.dumps(body), content_type="application/json"
        )

    def test_remind_by_assignment_id_and_student(self):
        resp = self.post(
            {"assignment_id": self.assignment.pk, "student_ids": [self.profile.pk]}
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.student, source_type="assessment_reminder"
            ).count(),
            1,
        )

    def test_remind_all_non_submitters_by_title(self):
        resp = self.post({"title": "Quiz 1"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["ok"])

    def test_remind_second_time(self):
        self.post({"assignment_id": self.assignment.pk})
        resp = self.post({"assignment_id": self.assignment.pk})
        self.assertEqual(resp.status_code, 200, resp.content)

    def student_summary(self):
        self.client.force_login(self.student)
        resp = self.client.get(reverse("communication:notification_summary"))
        self.client.force_login(self.teacher)
        return resp.json()

    def test_student_bell_sees_the_reminder(self):
        self.post({"assignment_id": self.assignment.pk})
        data = self.student_summary()
        self.assertGreaterEqual(data["unread_count"], 1)
        reminders = [
            i for i in data["items"] if i["type"] == "assessment" and "Reminder" in i["title"]
        ]
        self.assertTrue(reminders, data["items"])

    def test_second_reminder_re_notifies_after_reading(self):
        self.post({"assignment_id": self.assignment.pk})
        self.student_summary()  # just reads the endpoint
        Notification.objects.filter(recipient=self.student).update(is_read=True)
        self.post({"assignment_id": self.assignment.pk})
        data = self.student_summary()
        self.assertGreaterEqual(
            data["unread_count"], 1,
            "a fresh nudge must raise a new red dot, not silently reuse the read row",
        )


class RemindAdminTests(TestCase):
    """A leader (group admin, not Django is_staff) may open a classroom and
    should be able to send a reminder for it, exactly like they can read it."""

    def setUp(self):
        from django.contrib.auth.models import Group

        self.year = AcademicYear.objects.create(
            label="2026 GA",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            status="ACTIVE",
        )
        self.section = Section.objects.create(
            academic_year=self.year, name="JHS - A", grade_level=7
        )
        self.subject = Subject.objects.create(
            academic_year=self.year, name="Mathematics", grade_level=7
        )
        self.teacher = User.objects.create_user(email="t@tass.local", password="pw")
        self.vc = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
        )
        self.student, self.profile = _student("s_alice", self.year, 1)
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=self.vc, student=self.profile
        )
        self.assessment = Assessment.objects.create(
            subject=self.subject,
            academic_year=self.year,
            created_by=self.teacher,
            title="Quiz 1",
            assessment_method=Assessment.METHOD_QUIZ,
            assessment_type=Assessment.TYPE_WRITTEN_ORAL,
            status=Assessment.STATUS_ACTIVE,
            submission_deadline=timezone.now() + datetime.timedelta(days=3),
        )
        self.assignment = AssessmentAssignment.objects.create(
            assessment=self.assessment, virtual_classroom=self.vc, assigned_by=self.teacher
        )

        self.principal = User.objects.create_user(
            email="principal@tass.local", password="pw"
        )
        self.principal.status = User.Status.ACTIVE
        self.principal.save()
        self.principal.groups.add(Group.objects.create(name="principal"))
        self.assertFalse(self.principal.is_staff)
        self.client.force_login(self.principal)
        self.url = reverse("api_remind")

    def test_leader_can_remind_a_classroom_they_can_read(self):
        resp = self.client.post(
            self.url,
            data=json.dumps(
                {"assignment_id": self.assignment.pk, "student_ids": [self.profile.pk]}
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.student, source_type="assessment_reminder"
            ).count(),
            1,
        )
