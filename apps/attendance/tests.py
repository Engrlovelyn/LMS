"""Attendance workflow regression tests.

Covers the presentation-feedback fixes:

* an "Absent - Excused" record must carry a note or an attachment (enforced in
  ``AttendanceRecord.clean``), and the bulk recorder must surface that as a
  readable error rather than a crash or a bogus success;
* the per-student edit sheet renders each form outside the table and associates
  its controls with the HTML5 ``form`` attribute, so a ``<form>`` never has to
  straddle ``<td>`` boundaries.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.accounts.models import StudentProfile
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

User = get_user_model()

PASSWORD = "pass12345x"


class AttendanceWorkflowTestCase(TestCase):
    def setUp(self):
        self.year = AcademicYear.objects.create(
            label="2026-2027",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 3, 31),
        )
        self.section = Section.objects.create(
            academic_year=self.year, name="STEM-Newton", grade_level=11
        )
        self.subject = Subject.objects.create(
            academic_year=self.year,
            name="Elective A",
            grade_level=11,
            subject_type=Subject.TYPE_ELECTIVE,
        )
        self.teacher = User.objects.create_user(email="teacher@test.ph", password=PASSWORD)
        self.admin = User.objects.create_superuser(email="admin@test.ph", password=PASSWORD)
        self.classroom = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
            grading_period="1ST_TRIMESTER",
        )
        student_user = User.objects.create_user(email="student@test.ph", password=PASSWORD)
        self.student = StudentProfile.objects.create(
            user=student_user,
            lrn="123456789012",
            first_name="Juan",
            last_name="Cruz",
            birthdate=date(2009, 1, 1),
            sex="M",
        )
        VirtualClassroomEnrollment.objects.create(
            virtual_classroom=self.classroom, student=self.student
        )
        self.client.force_login(self.admin)

    def _record_url(self):
        return reverse("attendance:record_attendance", args=[self.classroom.pk])

    def _session(self, day=2):
        return AttendanceSession.objects.create(
            virtual_classroom=self.classroom,
            session_date=date(2026, 6, day),
            created_by=self.teacher,
        )

    def _messages(self, response):
        return [str(m) for m in response.context["messages"]]

    def test_excused_without_note_is_rejected(self):
        """An excused absence with no note or file saves nothing and explains why."""
        response = self.client.post(
            self._record_url(),
            {
                "session_date": "2026-06-02",
                "student_{}-status".format(self.student.pk): AttendanceRecord.ABSENT_EXCUSED,
            },
            follow=True,
        )
        self.assertFalse(
            AttendanceRecord.objects.filter(student=self.student).exists()
        )
        self.assertTrue(
            any("note or a supporting attachment" in m for m in self._messages(response))
        )

    def test_excused_with_note_saves(self):
        self.client.post(
            self._record_url(),
            {
                "session_date": "2026-06-03",
                "student_{}-status".format(self.student.pk): AttendanceRecord.ABSENT_EXCUSED,
                "student_{}-remarks".format(self.student.pk): "Medical certificate on file.",
            },
            follow=True,
        )
        record = AttendanceRecord.objects.get(student=self.student)
        self.assertEqual(record.status, AttendanceRecord.ABSENT_EXCUSED)
        self.assertEqual(record.remark, "Medical certificate on file.")

    def test_empty_submission_warns_instead_of_reporting_success(self):
        response = self.client.post(
            self._record_url(), {"session_date": "2026-06-04"}, follow=True
        )
        messages = self._messages(response)
        self.assertFalse(AttendanceRecord.objects.exists())
        self.assertTrue(any("No attendance records were saved" in m for m in messages))
        self.assertFalse(any("Saved attendance" in m for m in messages))

    def test_record_attendance_page_renders(self):
        """The bulk sheet compiles, including the inline excuse-validation block."""
        self._session(day=8)
        response = self.client.get(self._record_url() + "?date=2026-06-08")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "absent_excused")

    def test_session_update_associates_controls_by_form_attribute(self):
        session = self._session(day=5)
        response = self.client.get(
            reverse("attendance:session_update", args=[session.pk])
        )
        self.assertEqual(response.status_code, 200)
        # The per-student form lives outside the table and is targeted by id...
        self.assertContains(response, 'id="rowform-{}"'.format(self.student.pk))
        # ...and each control points back at it with the HTML5 `form` attribute.
        self.assertContains(response, 'form="rowform-{}"'.format(self.student.pk))

    def test_status_update_requires_note_for_excused(self):
        session = self._session(day=6)
        response = self.client.post(
            reverse("attendance:record_status_update", args=[session.pk]),
            {
                "student_id": self.student.pk,
                "status": AttendanceRecord.ABSENT_EXCUSED,
            },
            follow=True,
        )
        self.assertFalse(AttendanceRecord.objects.filter(session=session).exists())
        self.assertTrue(
            any("note or a supporting attachment" in m for m in self._messages(response))
        )
