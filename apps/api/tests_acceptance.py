"""Panel acceptance checks driven through the JSON bridge.

Three behaviours the panel signed off on are proven here end to end, because
each of them used to work only in the browser's localStorage:

* a topic a teacher adds is persisted, so it is still there (and in the
  classroom payload every dashboard paints from) after a refresh;
* attendance saved for a date can be read back for that date, so the calendar
  is rebuilt from the database instead of the browser;
* every learning material is labelled with its real stored size ("2.0 KB"),
  not with its filename extension.
"""

import datetime
import io
import json
import os
import shutil
import tempfile
import zipfile
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, RoleAssignment, StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.assessments.models import (
    Assessment,
    AssessmentAssignment,
    AssessmentPart,
    Submission,
)
from apps.dashboards.context import principal_context
from apps.teaching_learning.models import (
    LearningResource,
    LearningResourcePlacement,
    Topic,
)
from apps.teaching_learning.utils import human_size
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment


def _student(tag, lrn, first, last, sex="M"):
    user = User.objects.create_user(email="{}@stu.test".format(tag), password="pw")
    user.status = User.Status.ACTIVE
    user.save()
    return StudentProfile.objects.create(
        user=user,
        lrn=lrn,
        first_name=first,
        last_name=last,
        sex=sex,
        birthdate=datetime.date(2012, 3, 4),
    )


class TeacherContentAndAttendanceTests(TestCase):
    """Criteria 3 and 4: topics persist, attendance round-trips."""

    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2027, 5, 31),
            status=AcademicYear.STATUS_ACTIVE,
        )
        cls.section = Section.objects.create(
            academic_year=cls.year, name="Rizal", grade_level=7
        )
        cls.subject = Subject.objects.create(
            academic_year=cls.year, name="Science", grade_level=7
        )
        teacher_group = Group.objects.get_or_create(name="teacher")[0]
        cls.teacher = User.objects.create_user(
            email="accept-teacher@tass.test", password="pw"
        )
        cls.teacher.status = User.Status.ACTIVE
        cls.teacher.save()
        cls.teacher.groups.add(teacher_group)
        RoleAssignment.objects.create(
            user=cls.teacher,
            role=Role.objects.get_or_create(code=Role.Code.TEACHER)[0],
            academic_year=cls.year,
        )
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section,
            subject=cls.subject,
            teacher=cls.teacher,
        )
        cls.student_a = _student("accept-a", "136100200001", "Ana", "Abad")
        cls.student_b = _student("accept-b", "136100200002", "Ben", "Bautista")
        for profile in (cls.student_a, cls.student_b):
            VirtualClassroomEnrollment.objects.create(
                virtual_classroom=cls.vc, student=profile
            )

        # A second teacher with a classroom of their own, to prove both the
        # attendance read and the topic write stay inside the caller's scope.
        cls.other_teacher = User.objects.create_user(
            email="accept-other@tass.test", password="pw"
        )
        cls.other_teacher.status = User.Status.ACTIVE
        cls.other_teacher.save()
        cls.other_teacher.groups.add(teacher_group)
        other_section = Section.objects.create(
            academic_year=cls.year, name="Bonifacio", grade_level=7
        )
        cls.other_vc = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=other_section,
            subject=cls.subject,
            teacher=cls.other_teacher,
        )

    def setUp(self):
        self.client.force_login(self.teacher)

    # ── criterion 3: a new topic is saved, so it appears immediately ──────
    def test_new_topic_is_persisted_and_returned_by_the_classroom_payload(self):
        response = self.client.post(
            reverse("api_teacher_content"),
            {
                "action": "topic",
                "name": "Cell Division",
                "virtual_classroom_id": self.vc.pk,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["topic_id"])

        self.assertTrue(
            Topic.objects.filter(
                virtual_classroom=self.vc, name="Cell Division"
            ).exists()
        )

        payload = self.client.get(
            reverse("api_classroom_detail", args=[self.vc.pk])
        ).json()
        self.assertIn("Cell Division", [t["name"] for t in payload["topics"]])

    def test_topic_is_not_added_to_another_teachers_classroom(self):
        response = self.client.post(
            reverse("api_teacher_content"),
            {
                "action": "topic",
                "name": "Hijacked",
                "virtual_classroom_id": self.other_vc.pk,
            },
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(Topic.objects.filter(name="Hijacked").exists())

    def test_assessment_with_questions_ignores_empty_parts(self):
        response = self.client.post(
            reverse("api_assessments"),
            data=json.dumps({
                "virtual_classroom_id": self.vc.pk,
                "title": "Three Question Quiz",
                "type": "Quiz",
                "parts": [
                    {"id": "p1", "label": "Part I", "qtype": "mc"},
                    {"id": "p2", "label": "Unused Part", "qtype": "tf"},
                ],
                "questions": [
                    {
                        "partId": "p1",
                        "type": "mc",
                        "text": "Question {}".format(index),
                        "options": ["A", "B"],
                        "correct": 0,
                    }
                    for index in range(1, 4)
                ],
            }),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content.decode())
        assessment = Assessment.objects.get(pk=response.json()["assessment_id"])
        self.assertEqual(assessment.parts.count(), 1)
        self.assertEqual(len(assessment.parts.get().items), 3)

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_teacher_can_create_topic_upload_material_and_publish_without_permission_rows(self):
        Group.objects.get(name="teacher").permissions.clear()
        self.assertEqual(
            self.client.get(reverse("teaching:topic_add", args=[self.vc.pk])).status_code,
            200,
        )
        self.assertEqual(self.client.get(reverse("teaching:resource_add")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("communication:class_announcement_add_for", args=[self.vc.pk])).status_code,
            200,
        )
        topic_response = self.client.post(
            reverse("api_teacher_content"),
            {"action": "topic", "name": "Unit 2", "virtual_classroom_id": self.vc.pk},
        )
        self.assertEqual(topic_response.status_code, 200, topic_response.content)
        topic_id = topic_response.json()["topic_id"]
        upload_response = self.client.post(
            reverse("api_teacher_content"),
            {
                "action": "file", "type": "scorm", "topic_id": topic_id,
                "virtual_classroom_id": self.vc.pk,
                "file": SimpleUploadedFile("unit-2.zip", b"scorm-package"),
            },
        )
        self.assertEqual(upload_response.status_code, 200, upload_response.content)
        resource = LearningResource.objects.get(pk=upload_response.json()["resource_id"])
        self.assertTrue(
            LearningResourcePlacement.objects.filter(
                virtual_classroom=self.vc, topic_id=topic_id, resource=resource
            ).exists()
        )
        publish_response = self.client.post(
            reverse("api_assessments"),
            data=json.dumps({
                "virtual_classroom_id": self.vc.pk,
                "title": "Unit 2 Quiz",
                "type": "Quiz",
                "openDate": timezone.now().isoformat(),
                "deadline": (timezone.now() + datetime.timedelta(days=2)).isoformat(),
                "questions": [{"type": "mc", "text": "Question?", "options": ["A", "B"], "correct": 0}],
            }),
            content_type="application/json",
        )
        self.assertEqual(publish_response.status_code, 200, publish_response.content)
        assessment = Assessment.objects.get(pk=publish_response.json()["assessment_id"])
        self.assertTrue(AssessmentAssignment.objects.filter(assessment=assessment, virtual_classroom=self.vc).exists())
        self.assertEqual(
            self.client.get(reverse("assessments:assessment_update", args=[assessment.pk])).status_code,
            200,
        )
        self.assertEqual(self.client.get(reverse("assessments:assessment_repository")).status_code, 200)
        assessment.available_at = timezone.now() - datetime.timedelta(days=3)
        assessment.submission_deadline = timezone.now() - datetime.timedelta(days=1)
        assessment.save(update_fields=["available_at", "submission_deadline"])
        from apps.dashboards.context import teacher_context

        teacher_assessment = next(
            row for row in teacher_context(self.teacher)["dashboard_json"]["assessments"]
            if row["id"] == assessment.pk
        )
        self.assertTrue(teacher_assessment["isClosed"])
        reopen_deadline = timezone.now() + datetime.timedelta(days=2)
        reopen_payload = {
            "virtual_classroom_id": self.vc.pk,
            "assessment_id": assessment.pk,
            "title": assessment.title,
            "type": "Quiz",
            "openDate": timezone.now().isoformat(),
            "deadline": reopen_deadline.isoformat(),
        }
        keep_closed = self.client.post(
            reverse("api_assessments"),
            data=json.dumps(reopen_payload),
            content_type="application/json",
        )
        self.assertEqual(keep_closed.status_code, 400, keep_closed.content)
        reopen_response = self.client.post(
            reverse("api_assessments"),
            data=json.dumps(dict(reopen_payload, reopen=True)),
            content_type="application/json",
        )
        self.assertEqual(reopen_response.status_code, 200, reopen_response.content)
        assessment.refresh_from_db()
        self.assertGreater(assessment.submission_deadline, timezone.now())
        teacher_assessment = next(
            row for row in teacher_context(self.teacher)["dashboard_json"]["assessments"]
            if row["id"] == assessment.pk
        )
        self.assertFalse(teacher_assessment["isClosed"])

    def test_persistent_teacher_role_can_publish_even_without_a_teacher_group(self):
        self.teacher.groups.remove(Group.objects.get(name="teacher"))
        publish_response = self.client.post(
            reverse("api_assessments"),
            data=json.dumps({
                "virtual_classroom_id": self.vc.pk,
                "title": "Role-backed Quiz",
                "type": "Quiz",
                "openDate": timezone.now().isoformat(),
                "deadline": (timezone.now() + datetime.timedelta(days=2)).isoformat(),
                "questions": [{"type": "mc", "text": "Question?", "options": ["A", "B"], "correct": 0}],
            }),
            content_type="application/json",
        )

        self.assertEqual(publish_response.status_code, 200, publish_response.content)
        self.assertTrue(
            Assessment.objects.filter(
                pk=publish_response.json()["assessment_id"],
                created_by=self.teacher,
            ).exists()
        )

    def test_assessment_builder_persists_multiple_parts_matching_and_time_limit(self):
        payload = {
            "virtual_classroom_id": self.vc.pk,
            "title": "Multi-part Quiz",
            "type": "Quiz",
            "openDate": (timezone.now() - datetime.timedelta(minutes=1)).isoformat(),
            "deadline": (timezone.now() + datetime.timedelta(days=2)).isoformat(),
            "timeLimit": 12,
            "passingScore": 75,
            "allowSubmissionAttachments": True,
            "acceptedFileFormat": ".pdf,.docx",
            "allowSubmissionLinks": True,
            "revealCorrectAnswers": False,
            "shareToRepository": True,
            "parts": [
                {"id": "p100", "label": "Part I - Choices", "qtype": "mc"},
                {"id": "p101", "label": "Part II - Matching", "qtype": "mt"},
                {"id": "p102", "label": "Part III - Numeric", "qtype": "numeric"},
            ],
            "questions": [
                {
                    "partId": "p100",
                    "type": "mc",
                    "text": "Pick the first option",
                    "options": ["A", "B"],
                    "correct": 0,
                    "points": 2,
                },
                {
                    "partId": "p101",
                    "type": "mt",
                    "text": "Match each item",
                    "match": "Sun = Star\nEarth = Planet",
                    "points": 3,
                },
                {
                    "partId": "p102",
                    "type": "numeric",
                    "text": "What is 10 divided by 3?",
                    "answer": "3.33",
                    "tolerance": "0.01",
                    "points": 2,
                },
            ],
        }
        response = self.client.post(
            reverse("api_assessments"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content.decode())
        assessment = Assessment.objects.get(pk=response.json()["assessment_id"])
        parts = list(assessment.parts.order_by("order"))
        self.assertEqual(assessment.time_limit_minutes, 12)
        self.assertEqual(assessment.passing_score_percentage, 75)
        self.assertTrue(assessment.allow_submission_attachments)
        self.assertEqual(assessment.accepted_file_format, ".pdf,.docx")
        self.assertTrue(assessment.allow_submission_links)
        self.assertFalse(assessment.reveal_correct_answers)
        self.assertTrue(assessment.share_to_repository)
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0].items["item_1"]["question"], "Pick the first option")
        self.assertEqual(
            parts[1].items["item_1"]["correct_matches"],
            {"Sun": "Star", "Earth": "Planet"},
        )
        self.assertEqual(parts[2].items["item_1"]["correct_answer"], "3.33")
        self.assertEqual(parts[2].items["item_1"]["tolerance"], 0.01)

        self.student_a.user.user_permissions.add(
            Permission.objects.get(codename="complete_assessment_custom")
        )
        self.client.force_login(self.student_a.user)
        student_payload = self.client.get(
            reverse("assessments:assessment_submit", args=[assessment.pk]),
            {"assignment_id": assessment.assignments.get().pk, "format": "json"},
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(student_payload.status_code, 200, student_payload.content.decode())
        self.assertEqual(len(student_payload.json()["parts"]), 3)
        self.assertEqual(
            len(student_payload.json()["parts"][1]["questions"][0]["fields"]),
            2,
        )
        self.assertEqual(
            student_payload.json()["parts"][2]["questions"][0]["fields"][0]["kind"],
            "number",
        )

        from apps.assessments.scheduling import assessment_time_limit

        self.assertEqual(assessment_time_limit(assessment), datetime.timedelta(minutes=12))

    def test_assessment_edit_updates_questions_and_preserves_history(self):
        payload = {
            "virtual_classroom_id": self.vc.pk,
            "title": "Editable Quiz",
            "type": "Quiz",
            "openDate": (timezone.now() - datetime.timedelta(minutes=1)).isoformat(),
            "deadline": (timezone.now() + datetime.timedelta(days=2)).isoformat(),
            "parts": [{"id": "p200", "label": "Part I", "qtype": "mc"}],
            "questions": [
                {
                    "partId": "p200",
                    "type": "mc",
                    "text": "Original prompt",
                    "options": ["A", "B"],
                    "correct": 0,
                    "points": 1,
                }
            ],
        }
        created = self.client.post(
            reverse("api_assessments"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 200, created.content.decode())
        assessment = Assessment.objects.get(pk=created.json()["assessment_id"])
        part = assessment.parts.get()
        edited_payload = dict(payload)
        edited_payload.update(
            assessment_id=assessment.pk,
            parts=[{"id": "p{}".format(part.pk), "label": "Part I", "qtype": "mc"}],
            questions=[
                {
                    "partId": "p{}".format(part.pk),
                    "itemKey": "item_1",
                    "type": "mc",
                    "text": "Updated prompt",
                    "options": ["A", "B", "C"],
                    "correct": 2,
                    "points": 4,
                },
                {
                    "partId": "p{}".format(part.pk),
                    "type": "mc",
                    "text": "Added prompt",
                    "options": ["Yes", "No"],
                    "correct": 1,
                    "points": 2,
                },
            ],
        )
        updated = self.client.post(
            reverse("api_assessments"),
            data=json.dumps(edited_payload),
            content_type="application/json",
        )
        self.assertEqual(updated.status_code, 200, updated.content.decode())
        part.refresh_from_db()
        self.assertEqual(part.items["item_1"]["question"], "Updated prompt")
        self.assertEqual(part.items["item_1"]["correct_answer"], "C")
        self.assertEqual(part.items["item_2"]["question"], "Added prompt")
        self.assertEqual(part.points, 6)

        Submission.objects.create(
            assessment_assignment=AssessmentAssignment.objects.get(
                assessment=assessment, virtual_classroom=self.vc
            ),
            student=self.student_a,
        )
        edited_payload["questions"][0]["text"] = "Unsafe historical edit"
        rejected = self.client.post(
            reverse("api_assessments"),
            data=json.dumps(edited_payload),
            content_type="application/json",
        )
        self.assertEqual(rejected.status_code, 409, rejected.content.decode())
        part.refresh_from_db()
        self.assertEqual(part.items["item_1"]["question"], "Updated prompt")

    # ── criterion 4: attendance is saved and can be read back ─────────────
    def _save_attendance(self, day, records):
        return self.client.post(
            reverse("api_attendance"),
            data=json.dumps(
                {
                    "virtual_classroom_id": self.vc.pk,
                    "date": day.isoformat(),
                    "records": records,
                }
            ),
            content_type="application/json",
        )

    def _calendar(self, day):
        response = self.client.get(
            reverse("api_attendance_calendar"),
            {"from": day.isoformat(), "to": day.isoformat()},
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["calendar"]

    def test_saved_attendance_is_read_back_for_the_same_date(self):
        day = timezone.localdate().replace(day=8)
        result = self._save_attendance(
            day,
            [
                {"lrn": self.student_a.lrn, "name": "Ana Abad", "status": "present"},
                {"lrn": self.student_b.lrn, "name": "Ben Bautista", "status": "late"},
            ],
        )
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["count"], 2)
        from apps.communication.models import Notification

        self.assertEqual(
            Notification.objects.get(
                recipient=self.student_a.user,
                notification_type=Notification.TYPE_ATTENDANCE,
            ).title,
            "Marked Present",
        )

        calendar = self._calendar(day)
        self.assertEqual(calendar[day.isoformat()][str(self.student_a.pk)], "P")
        self.assertEqual(calendar[day.isoformat()][str(self.student_b.pk)], "L")
        self.assertEqual(
            calendar[day.isoformat() + ".sub"][str(self.student_b.pk)]["sub"], "late"
        )

    def test_resaving_attendance_updates_the_existing_session(self):
        day = timezone.localdate().replace(day=9)
        self._save_attendance(
            day,
            [{"lrn": self.student_b.lrn, "name": "Ben Bautista", "status": "absent"}],
        )
        self._save_attendance(
            day,
            [
                {
                    "lrn": self.student_b.lrn,
                    "name": "Ben Bautista",
                    "status": "excused",
                    "remark": "Medical certificate",
                }
            ],
        )

        self.assertEqual(
            AttendanceSession.objects.filter(
                virtual_classroom=self.vc, session_date=day
            ).count(),
            1,
        )
        self.assertEqual(
            AttendanceRecord.objects.filter(session__session_date=day).count(), 1
        )
        calendar = self._calendar(day)
        self.assertEqual(calendar[day.isoformat()][str(self.student_b.pk)], "A")
        self.assertEqual(
            calendar[day.isoformat() + ".sub"][str(self.student_b.pk)]["sub"], "excused"
        )
        self.assertEqual(
            calendar[day.isoformat() + ".sub"][str(self.student_b.pk)]["remark"],
            "Medical certificate",
        )

    def test_another_teachers_attendance_is_not_read(self):
        day = timezone.localdate().replace(day=10)
        session = AttendanceSession.objects.create(
            virtual_classroom=self.other_vc,
            session_date=day,
            created_by=self.other_teacher,
        )
        AttendanceRecord.objects.create(
            session=session,
            student=self.student_a,
            status=AttendanceRecord.PRESENT_ON_TIME,
        )
        self.assertEqual(self._calendar(day), {})

    def test_calendar_range_is_capped(self):
        response = self.client.get(
            reverse("api_attendance_calendar"),
            {"from": "2026-01-01", "to": "2026-12-31"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        span = datetime.date.fromisoformat(body["to_date"]) - datetime.date.fromisoformat(
            body["from_date"]
        )
        self.assertLessEqual(span.days, 365)
        self.assertEqual(body["to_date"], "2026-12-31")


class LearningMaterialSizeTests(TestCase):
    """Criterion 6: the label is the stored file's size in KB/MB."""

    @classmethod
    def setUpTestData(cls):
        call_command("assign_role_permissions", verbosity=0)
        cls.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2027, 5, 31),
            status=AcademicYear.STATUS_ACTIVE,
        )
        cls.section = Section.objects.create(
            academic_year=cls.year, name="Mabini", grade_level=8
        )
        cls.subject = Subject.objects.create(
            academic_year=cls.year, name="English", grade_level=8
        )
        teacher_group = Group.objects.get_or_create(name="teacher")[0]
        cls.teacher = User.objects.create_user(
            email="size-teacher@tass.test", password="pw"
        )
        cls.teacher.status = User.Status.ACTIVE
        cls.teacher.save()
        cls.teacher.groups.add(teacher_group)
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section,
            subject=cls.subject,
            teacher=cls.teacher,
        )
        cls.principal = User.objects.create_user(
            email="size-principal@tass.test", password="pw"
        )
        cls.principal.status = User.Status.ACTIVE
        cls.principal.save()
        cls.principal.groups.add(Group.objects.get_or_create(name="principal")[0])

    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="tass-size-")
        # Keep acceptance tests independent of production R2 credentials and
        # network access. The model's FileField otherwise uses the configured
        # Cloudflare storage even when MEDIA_ROOT points at this temp folder.
        self.override = override_settings(
            MEDIA_ROOT=self.media_root,
            STORAGES={
                "default": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                },
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
                },
            },
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, True)

        upload = SimpleUploadedFile(
            "cell-notes.pdf", b"x" * 2048, content_type="application/pdf"
        )
        self.resource = LearningResource.objects.create(
            subject=self.subject,
            academic_year=self.year,
            uploaded_by=self.teacher,
            title="Cell Notes",
            resource_type="pdf",
            file=upload,
            share_to_repository=True,
        )
        topic = Topic.objects.create(virtual_classroom=self.vc, name="Cells", order=1)
        LearningResourcePlacement.objects.create(
            resource=self.resource,
            virtual_classroom=self.vc,
            topic=topic,
            placed_by=self.teacher,
        )

    def test_classroom_payload_reports_bytes_and_a_kb_label(self):
        self.client.force_login(self.teacher)
        payload = self.client.get(
            reverse("api_classroom_detail", args=[self.vc.pk])
        ).json()
        row = next(r for r in payload["resources"] if r["title"] == "Cell Notes")
        self.assertEqual(row["size"], 2048)
        self.assertEqual(row["sizeLabel"], "2.0 KB")

    def test_size_formatter_uses_mb_for_megabyte_files(self):
        self.assertEqual(human_size(2 * 1024 * 1024), "2.0 MB")

    def test_repository_endpoint_labels_the_size_not_the_extension(self):
        self.client.force_login(self.teacher)
        payload = self.client.get(reverse("api_learning_resources")).json()
        row = next(r for r in payload["resources"] if r["id"] == self.resource.pk)
        self.assertIn("2.0 KB", row["meta"])

    def test_oversight_repository_labels_the_size(self):
        payload = principal_context(self.principal)["dashboard_json"]
        row = next(m for m in payload["materials"] if m["name"] == "Cell Notes")
        self.assertIn("2.0 KB", row["meta"])

    def test_link_resources_have_no_size_label(self):
        link = LearningResource.objects.create(
            subject=self.subject,
            academic_year=self.year,
            uploaded_by=self.teacher,
            title="Reference Link",
            resource_type="link",
            external_link="https://example.com/notes",
            share_to_repository=True,
        )
        self.client.force_login(self.teacher)
        payload = self.client.get(reverse("api_learning_resources")).json()
        row = next(r for r in payload["resources"] if r["id"] == link.pk)
        self.assertNotIn(" KB", row["meta"])
        self.assertNotIn(" MB", row["meta"])

    def test_teacher_resource_repository_includes_all_subjects_in_active_year(self):
        other_subject = Subject.objects.create(
            academic_year=self.year, name="Filipino", grade_level=8
        )
        other_resource = LearningResource.objects.create(
            subject=other_subject,
            academic_year=self.year,
            uploaded_by=self.teacher,
            title="Other subject resource",
            resource_type="link",
            external_link="https://example.com/other",
            share_to_repository=True,
        )
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("api_learning_resources"))
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        ids = {row["id"] for row in payload["resources"]}
        subject_ids = {row["id"] for row in payload["repository_subjects"]}
        self.assertIn(self.resource.pk, ids)
        self.assertIn(other_resource.pk, ids)
        self.assertIn(other_subject.pk, subject_ids)

    def test_old_year_shared_resource_remains_available_and_can_be_reused(self):
        old_year = AcademicYear.objects.create(
            label="2025-2026",
            start_date=datetime.date(2025, 6, 1),
            end_date=datetime.date(2026, 5, 31),
            status=AcademicYear.STATUS_ARCHIVED,
        )
        old_subject = Subject.objects.create(
            academic_year=old_year, name="English", grade_level=8
        )
        archived_resource = LearningResource.objects.create(
            subject=old_subject,
            academic_year=old_year,
            uploaded_by=self.teacher,
            title="Archived English Notes",
            resource_type="link",
            external_link="https://example.com/archived-notes",
            share_to_repository=True,
        )
        self.client.force_login(self.teacher)

        repository = self.client.get(
            reverse("api_learning_resources"), {"academic_year_id": old_year.pk}
        ).json()
        self.assertIn(
            archived_resource.pk,
            {row["id"] for row in repository["resources"]},
        )
        response = self.client.post(
            reverse("api_learning_resource_attach"),
            {
                "resource_id": archived_resource.pk,
                "virtual_classroom_id": self.vc.pk,
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(
            LearningResourcePlacement.objects.filter(
                resource=archived_resource, virtual_classroom=self.vc
            ).exists()
        )
        archived_resource.refresh_from_db()
        self.assertEqual(archived_resource.subject, old_subject)

    def test_repository_subject_list_includes_assigned_subjects_with_no_materials(self):
        subject = Subject.objects.create(
            academic_year=self.year, name="Mathematics 1", grade_level=8
        )
        section = Section.objects.create(
            academic_year=self.year, name="Bonifacio", grade_level=8
        )
        VirtualClassroom.objects.create(
            academic_year=self.year,
            section=section,
            subject=subject,
            teacher=self.teacher,
        )

        self.client.force_login(self.teacher)
        payload = self.client.get(reverse("api_learning_resources")).json()
        repository_subjects = {
            row["id"]: row["name"] for row in payload["repository_subjects"]
        }
        self.assertEqual(repository_subjects[subject.pk], "Mathematics 1")
        self.assertFalse(
            any(row["subject_id"] == subject.pk for row in payload["resources"])
        )

    def test_docx_preview_preserves_document_structure_and_escapes_content(self):
        document_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Lesson Template</w:t></w:r></w:p>
            <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Bold section</w:t></w:r><w:r><w:t>&lt;script&gt;bad()&lt;/script&gt;</w:t></w:r></w:p>
            <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
            <w:sectPr/>
          </w:body>
        </w:document>'''
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as docx:
            docx.writestr("word/document.xml", document_xml)
        resource = LearningResource.objects.create(
            subject=self.subject,
            academic_year=self.year,
            uploaded_by=self.teacher,
            title="Lesson Template",
            resource_type="document",
            file=SimpleUploadedFile(
                "lesson-template.docx", archive.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            share_to_repository=True,
        )
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("teaching:resource_preview", args=[resource.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        preview = response.json()
        self.assertIn('<h2', preview["html"])
        self.assertIn("<strong>Bold section</strong>", preview["html"])
        self.assertIn("<table>", preview["html"])
        self.assertIn("Table cell", preview["html"])
        self.assertNotIn("<script>", preview["html"])
        self.assertIn("&lt;script&gt;", preview["html"])

    def test_student_material_preview_page_has_a_stable_refreshable_url(self):
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("teaching:resource_preview_page", args=[self.resource.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.resource.title)
        self.assertContains(response, reverse("teaching:resource_preview", args=[self.resource.pk]))
        self.assertContains(response, "document_preview.bundle.js")
        self.assertContains(response, "tassRenderDocumentPreview")
        preview = self.client.get(reverse("teaching:resource_preview", args=[self.resource.pk]))
        self.assertEqual(preview.status_code, 200)
        self.assertIn("preview_file_url", preview.json())
        self.assertEqual(
            preview.json()["download_url"],
            reverse("teaching:resource_download", args=[self.resource.pk]),
        )
        inline_file = self.client.get(
            reverse("teaching:resource_download", args=[self.resource.pk]),
            {"inline": "1"},
        )
        self.assertEqual(inline_file.status_code, 200)
        self.assertEqual(inline_file["Content-Type"], "application/pdf")
        self.assertEqual(inline_file["X-Frame-Options"], "SAMEORIGIN")

    @patch("apps.teaching_learning.views.shutil.which", return_value="soffice")
    def test_office_preview_exposes_pdf_route_when_converter_is_available(self, _which):
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("teaching:resource_preview", args=[self.resource.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["office_pdf_url"], "")  # PDFs render directly.

    @patch("apps.teaching_learning.views.subprocess.run")
    @patch("apps.teaching_learning.views._office_converter_binary", return_value="soffice")
    def test_office_conversion_returns_inline_pdf(self, _converter, run):
        document_xml = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Preview</w:t></w:r></w:p></w:body></w:document>'''
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as docx:
            docx.writestr("word/document.xml", document_xml)
        resource = LearningResource.objects.create(
            subject=self.subject,
            academic_year=self.year,
            uploaded_by=self.teacher,
            title="Lesson Preview",
            resource_type="document",
            file=SimpleUploadedFile("lesson-preview.docx", archive.getvalue()),
            share_to_repository=True,
        )

        def convert(command, **_kwargs):
            outdir = command[command.index("--outdir") + 1]
            with open(os.path.join(outdir, "material.pdf"), "wb") as converted:
                converted.write(b"%PDF-1.4\\npreview\\n%%EOF")
            return type("Completed", (), {"returncode": 0})()

        run.side_effect = convert
        self.client.force_login(self.teacher)
        preview_response = self.client.get(reverse("teaching:resource_preview", args=[resource.pk]))
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(
            preview_response.json()["office_pdf_url"],
            reverse("teaching:resource_office_pdf", args=[resource.pk]),
        )
        response = self.client.get(reverse("teaching:resource_office_pdf", args=[resource.pk]))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")
        self.assertTrue(response.content.startswith(b"%PDF-"))

    @patch("apps.teaching_learning.views._convert_with_word")
    @patch(
        "apps.teaching_learning.views._office_converter_binary",
        return_value=r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
    )
    def test_word_conversion_returns_original_layout_pdf(self, _converter, convert):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as docx:
            docx.writestr(
                "word/document.xml",
                b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Word preview</w:t></w:r></w:p></w:body></w:document>',
            )
        resource = LearningResource.objects.create(
            subject=self.subject,
            academic_year=self.year,
            uploaded_by=self.teacher,
            title="Word Layout Preview",
            resource_type="document",
            file=SimpleUploadedFile("word-layout.docx", archive.getvalue()),
            share_to_repository=True,
        )

        def convert_docx(source_path, output_path):
            with open(output_path, "wb") as converted:
                converted.write(b"%PDF-1.4\nword-rendered-preview\n%%EOF")
            return type("Completed", (), {"returncode": 0})()

        convert.side_effect = convert_docx
        self.client.force_login(self.teacher)
        preview = self.client.get(reverse("teaching:resource_preview", args=[resource.pk]))
        self.assertEqual(
            preview.json()["office_pdf_url"],
            reverse("teaching:resource_office_pdf", args=[resource.pk]),
        )
        response = self.client.get(preview.json()["office_pdf_url"])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))
        convert.assert_called_once()

    @patch("apps.teaching_learning.views._office_converter_binary", return_value=None)
    def test_office_pdf_route_rejects_non_office_files(self, _converter):
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("teaching:resource_office_pdf", args=[self.resource.pk]))
        self.assertEqual(response.status_code, 415)  # The fixture is a PDF, not a convertible Office file.

    @patch("apps.teaching_learning.views._office_converter_binary", return_value=None)
    def test_office_pdf_route_reports_missing_converter(self, _converter):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as docx:
            docx.writestr(
                "word/document.xml",
                b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Preview</w:t></w:r></w:p></w:body></w:document>',
            )
        resource = LearningResource.objects.create(
            subject=self.subject,
            academic_year=self.year,
            uploaded_by=self.teacher,
            title="Lesson Preview",
            resource_type="document",
            file=SimpleUploadedFile("lesson-preview.docx", archive.getvalue()),
            share_to_repository=True,
        )
        self.client.force_login(self.teacher)
        response = self.client.get(reverse("teaching:resource_office_pdf", args=[resource.pk]))
        self.assertEqual(response.status_code, 503)
