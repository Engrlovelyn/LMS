"""End-to-end proof that a teacher write reaches the right student's screen.

These drive the real pieces: the real ``/ws/live/`` socket, the real
``notify_*`` helpers, and real classroom/enrollment rows. The point of the suite
is the scoping guarantee - a change in section A must never surface on a
student enrolled in section B - plus the case where a student has not reloaded
since being enrolled and therefore has not joined the classroom room yet.
"""

import datetime
import json

from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser, Group
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, Subject
from apps.communication.access import messageable_contacts
from apps.communication.models import SchoolAnnouncement
from apps.communication.realtime import (
    notify_classroom,
    notify_enrolled_students,
    notify_school,
)
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment
from config.asgi import application

TEST_CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def _student_user(tag, year, n):
    """A minimal but valid student record. This project's User keys on email."""
    email = f"{tag}@tass.local"
    user = User.objects.create_user(email=email, password="pw")
    profile = StudentProfile.objects.create(
        user=user,
        lrn=f"LRN-{year.label}-{n:04d}",
        first_name=tag.title(),
        last_name="Student",
        birthdate=datetime.date(2009, 1, 1),
        sex="M",
    )
    return user, profile


def _enroll(profile, vc):
    return VirtualClassroomEnrollment.objects.create(
        virtual_classroom=vc, student=profile
    )


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class LiveSocketTests(TransactionTestCase):
    def setUp(self):
        self.year = AcademicYear.objects.create(
            label="2026 GA",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            status="ACTIVE",
        )
        self.section_a = Section.objects.create(
            academic_year=self.year, name="JHS - A", grade_level=7
        )
        self.section_b = Section.objects.create(
            academic_year=self.year, name="JHS - B", grade_level=7
        )
        self.subject = Subject.objects.create(
            academic_year=self.year, name="Mathematics", grade_level=7
        )

        self.teacher = User.objects.create_user(
            email="t_maths@tass.local", password="pw"
        )
        self.vc_a = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section_a,
            subject=self.subject,
            teacher=self.teacher,
        )
        self.vc_b = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section_b,
            subject=self.subject,
            teacher=self.teacher,
        )

        self.student_a, self.profile_a = _student_user("s_alice", self.year, 1)
        self.student_b, self.profile_b = _student_user("s_bob", self.year, 2)
        self.outsider, self.profile_out = _student_user("s_cara", self.year, 3)

        _enroll(self.profile_a, self.vc_a)
        _enroll(self.profile_b, self.vc_b)

    async def _open(self, user):
        communicator = WebsocketCommunicator(application, "/ws/live/")
        communicator.scope["user"] = user
        connected, _ = await communicator.connect()
        self.assertTrue(connected, f"{user} could not open /ws/live/")
        return communicator

    async def _db(self, fn, *args):
        """Run a synchronous ORM call from these async tests.

        The bodies are ``async def``, so any queryset evaluation on the event
        loop raises SynchronousOnlyOperation. The helpers under test are called
        directly on purpose -- they have to survive being driven from async
        context, since that is how the websocket layer calls them.
        """
        from channels.db import database_sync_to_async

        return await database_sync_to_async(fn)(*args)

    async def test_teacher_write_reaches_enrolled_student(self):
        comm = await self._open(self.student_a)
        notify_classroom(self.vc_a.pk, kind=["announcements"], actor=self.teacher)
        msg = await comm.receive_json_from(timeout=3)
        self.assertEqual(msg["type"], "live")
        self.assertEqual(msg["classroom_id"], self.vc_a.pk)
        self.assertIn("announcements", msg["kinds"])
        self.assertEqual(msg["actor"], self.teacher.email)
        await comm.disconnect()

    async def test_change_does_not_reach_another_classroom(self):
        """A change in section A must never appear on a section B student."""
        comm = await self._open(self.student_b)
        notify_classroom(self.vc_a.pk, kind=["announcements"], actor=self.teacher)
        self.assertTrue(
            await comm.receive_nothing(timeout=1.0),
            "a student in another classroom received a foreign classroom event",
        )
        await comm.disconnect()

    async def test_realtime_failure_never_breaks_a_save(self):
        """If the layer is broken the write must still succeed."""
        from apps.communication import realtime

        original = realtime._send
        realtime._send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redis down"))
        try:
            self.assertFalse(
                realtime.notify_classroom(self.vc_a.pk, kind=["materials"])
            )
        finally:
            realtime._send = original
        self.assertTrue(
            await self._db(
                VirtualClassroom.objects.filter(pk=self.vc_a.pk).exists
            ),
            "classroom row disappeared while realtime was failing",
        )

    async def test_newly_enrolled_student_is_nudged_directly(self):
        """A student who has not reloaded has not joined ``class-<id>`` yet.

        ``notify_enrolled_students`` therefore also pings the personal room;
        without that, a just-enrolled student would see nothing until a refresh.
        """
        await self._db(_enroll, self.profile_out, self.vc_a)
        comm = await self._open(self.outsider)

        notify_enrolled_students(self.vc_a.pk, kind=["materials"], actor=self.teacher)

        got_user_scope = False
        for _ in range(3):
            try:
                msg = await comm.receive_json_from(timeout=1.5)
            except Exception:
                break
            if msg.get("scope") == "user":
                got_user_scope = True
                break
        self.assertTrue(got_user_scope, "newly enrolled student got no direct notification")
        await comm.disconnect()

    async def test_school_wide_event_reaches_everyone(self):
        comm = await self._open(self.student_b)
        notify_school(kind=["school_announcements"], actor=self.teacher)
        msg = await comm.receive_json_from(timeout=3)
        self.assertEqual(msg["scope"], "school")
        self.assertIn("school_announcements", msg["kinds"])
        await comm.disconnect()

    async def test_anonymous_is_rejected(self):
        communicator = WebsocketCommunicator(application, "/ws/live/")
        communicator.scope["user"] = AnonymousUser()
        connected, code = await communicator.connect()
        self.assertFalse(connected, "anonymous user was allowed onto the live socket")
        self.assertIn(code, (403, 4401, 4403))


@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class SchoolAnnouncementAudienceTests(TestCase):
    """An Elementary Principal's post stays on the Elementary campus and the
    High School Principal's post stays on the High School campus, in both
    directions, for every read path: the module page, the dashboard feed and
    the JSON write endpoint."""

    @classmethod
    def setUpTestData(cls):
        from apps.accounts.models import Role, RoleAssignment
        from django.core.management import call_command

        call_command("assign_role_permissions", verbosity=0)

        cls.teacher = User.objects.create_user(email="faculty@tass.local", password="pw")
        cls.teacher.status = "active"
        cls.teacher.save()
        cls.year = AcademicYear.objects.create(
            label="2026 GA",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            status="ACTIVE",
        )
        cls.ann_elem = SchoolAnnouncement.objects.create(
            title="Elem Only", content="Lower campus bus reminder.",
            posted_by=cls.teacher, audience="elementary",
        )
        cls.ann_hs = SchoolAnnouncement.objects.create(
            title="HS Only", content="Upper campus gate closure.",
            posted_by=cls.teacher, audience="high_school",
        )
        cls.ann_all = SchoolAnnouncement.objects.create(
            title="Whole School", content="Flag ceremony.",
            posted_by=cls.teacher, audience="whole_school",
        )
        cls.role_ep = Role.objects.get_or_create(code="elementary_principal")[0]
        cls.role_hs = Role.objects.get_or_create(code="principal")[0]

    def _login_principal(self, email, role_code, role_obj):
        from apps.accounts.models import RoleAssignment
        from django.contrib.auth.models import Group

        user = User.objects.create_user(email=email, password="pw")
        user.status = "active"
        user.save()
        user.groups.add(Group.objects.get_or_create(name=role_code)[0])
        RoleAssignment.objects.create(user=user, role=role_obj, academic_year=self.year)
        self.client.login(username=email, password="pw")
        return user

    def _readable_titles(self, user):
        from apps.accounts.campus import filter_school_announcements

        qs = filter_school_announcements(SchoolAnnouncement.objects.all(), user)
        return list(qs.values_list("title", flat=True))

    def test_elem_principal_reads_only_its_campus_feed(self):
        user = self._login_principal("ep@tass.local", "elementary_principal", self.role_ep)
        titles = self._readable_titles(user)
        self.assertIn("Elem Only", titles)
        self.assertIn("Whole School", titles)
        self.assertNotIn("HS Only", titles)

    def test_hs_principal_reads_only_its_campus_feed(self):
        user = self._login_principal("hsp@tass.local", "principal", self.role_hs)
        titles = self._readable_titles(user)
        self.assertIn("HS Only", titles)
        self.assertIn("Whole School", titles)
        self.assertNotIn("Elem Only", titles)

    def test_module_page_is_campus_filtered(self):
        self._login_principal("hsp2@tass.local", "principal", self.role_hs)
        resp = self.client.get(reverse("communication:announcements"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "HS Only")
        self.assertContains(resp, "Whole School")
        self.assertNotContains(resp, "Elem Only")

    def test_elem_principal_cannot_post_to_high_school(self):
        from apps.accounts.campus import can_post_to_campus

        user = self._login_principal("ep2@tass.local", "elementary_principal", self.role_ep)
        self.assertTrue(can_post_to_campus(user, "elementary"))
        self.assertTrue(can_post_to_campus(user, "whole_school"))
        self.assertFalse(can_post_to_campus(user, "high_school"))

    def test_elem_principal_api_rejects_high_school_audience(self):
        self._login_principal("ep3@tass.local", "elementary_principal", self.role_ep)
        resp = self.client.post(
            reverse("api_school_announcements"),
            data={"announcements": [{"title": "Leak", "content": "Should be blocked", "audience": "high_school"}]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_school_announcement_form_blocks_foreign_campus(self):
        from apps.communication.forms import SchoolAnnouncementForm

        user = self._login_principal("ep4@tass.local", "elementary_principal", self.role_ep)
        form = SchoolAnnouncementForm(
            data={
                "title": "Bus route",
                "content": "Updated times.",
                "audience": "high_school",
                "status": "active",
            },
            user=user,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("audience", form.errors)


class NotificationBellTests(TestCase):
    """The persistent bell: scoped recipients, idempotent creation, and
    per-recipient read state, exercised through the service and the views."""

    @classmethod
    def setUpTestData(cls):
        cls.year = AcademicYear.objects.create(
            label="2026 NB",
            start_date=datetime.date(2026, 1, 1),
            end_date=datetime.date(2026, 12, 31),
            status="ACTIVE",
        )
        cls.section = Section.objects.create(
            academic_year=cls.year, name="NB - A", grade_level=7
        )
        cls.subject = Subject.objects.create(
            academic_year=cls.year, name="Science", grade_level=7
        )
        cls.teacher = User.objects.create_user(email="nb_teacher@tass.local", password="pw")
        cls.teacher.groups.add(Group.objects.get_or_create(name="teacher")[0])
        cls.vc = VirtualClassroom.objects.create(
            academic_year=cls.year,
            section=cls.section,
            subject=cls.subject,
            teacher=cls.teacher,
        )
        cls.student_a, cls.profile_a = _student_user("nb_alice", cls.year, 11)
        cls.student_b, cls.profile_b = _student_user("nb_bob", cls.year, 12)
        cls.outsider, cls.profile_out = _student_user("nb_cara", cls.year, 13)
        student_group, _ = Group.objects.get_or_create(name="student")
        for student in (cls.student_a, cls.student_b, cls.outsider):
            student.groups.add(student_group)
        for u in (cls.student_a, cls.student_b, cls.outsider, cls.teacher):
            u.status = "active"
            u.save(update_fields=["status", "is_active"])
        _enroll(cls.profile_a, cls.vc)
        _enroll(cls.profile_b, cls.vc)

    def _announcement(self, content="Hi class"):
        from apps.communication.models import ClassAnnouncement

        return ClassAnnouncement.objects.create(
            virtual_classroom=self.vc, posted_by=self.teacher, content=content
        )

    def test_announcement_notifies_enrolled_students_only(self):
        from apps.communication import notifications as svc
        from apps.communication.models import Notification

        ann = self._announcement()
        self.assertEqual(svc.notify_class_announcement(ann), 2)
        recipients = set(
            Notification.objects.filter(
                source_type="class_announcement", source_id=ann.pk
            ).values_list("recipient_id", flat=True)
        )
        self.assertEqual(recipients, {self.student_a.pk, self.student_b.pk})
        self.assertNotIn(self.outsider.pk, recipients)

    def test_announcement_notification_is_idempotent(self):
        from apps.communication import notifications as svc
        from apps.communication.models import Notification

        ann = self._announcement()
        svc.notify_class_announcement(ann)
        self.assertEqual(svc.notify_class_announcement(ann), 0)
        self.assertEqual(
            Notification.objects.filter(
                source_type="class_announcement", source_id=ann.pk
            ).count(),
            2,
        )

    def test_teacher_post_is_saved_with_title_and_visible_to_enrolled_student(self):
        from apps.communication.models import ClassAnnouncement, Notification

        endpoint = reverse("api_class_announcements")
        payload = {
            "virtual_classroom_id": self.vc.pk,
            "title": "Lab safety",
            "content": "Bring your safety goggles.",
            "target_role": "all",
        }
        self.client.login(username=self.teacher.email, password="pw")
        response = self.client.post(endpoint, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertTrue(result["ok"])
        announcement = ClassAnnouncement.objects.get(pk=result["announcement"]["id"])
        self.assertEqual(announcement.title, payload["title"])
        self.assertEqual(announcement.content, payload["content"])
        self.assertEqual(
            Notification.objects.filter(
                source_type="class_announcement", source_id=announcement.pk
            ).count(),
            2,
        )

        self.client.login(username=self.student_a.email, password="pw")
        visible = self.client.get(reverse("api_my_classroom", args=[self.vc.pk]))
        self.assertEqual(visible.status_code, 200, visible.content)
        rows = visible.json()["announcements"]
        self.assertTrue(any(
            row["announcement_id"] == announcement.pk
            and row["title"] == payload["title"]
            and row["body"] == payload["content"]
            for row in rows
        ))

    def test_summary_endpoint_is_recipient_scoped(self):
        from apps.communication import notifications as svc

        svc.notify_class_announcement(self._announcement())
        self.client.login(username=self.student_a.email, password="pw")
        resp = self.client.get(reverse("communication:notification_summary"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["unread_count"], 1)
        self.assertEqual(len(data["items"]), 1)
        self.assertTrue(data["items"][0]["open_url"])

    def test_teacher_notification_opens_the_active_dashboard_classroom_tab(self):
        from apps.communication.models import Notification

        ann = self._announcement()
        notification = Notification.objects.create(
            recipient=self.teacher,
            virtual_classroom=self.vc,
            notification_type=Notification.TYPE_ANNOUNCEMENT,
            source_type="class_announcement",
            source_id=ann.pk,
            title="Class announcement",
            message="New class announcement",
        )
        self.client.login(username=self.teacher.email, password="pw")

        summary = self.client.get(reverse("communication:notification_summary")).json()
        self.assertIn(
            "notification_classroom={}&notification_record_type=announcement&notification_record={}".format(
                self.vc.pk, ann.pk
            ),
            summary["items"][0]["url"],
        )
        opened = self.client.get(
            reverse("communication:notification_open", args=[notification.pk])
        )
        self.assertEqual(opened.status_code, 302)
        self.assertEqual(opened.url, summary["items"][0]["url"])
        self.assertIn(reverse("dashboards:teacher"), opened.url)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

        dashboard = self.client.get(reverse("dashboards:teacher"))
        self.assertContains(dashboard, "restoreTeacherNotificationTarget")

    def test_student_notification_opens_the_active_student_classroom_tab(self):
        from apps.communication.models import Notification

        ann = self._announcement()
        notification = Notification.objects.create(
            recipient=self.student_a,
            virtual_classroom=self.vc,
            notification_type=Notification.TYPE_ANNOUNCEMENT,
            source_type="class_announcement",
            source_id=ann.pk,
            title="Class announcement",
            message="New class announcement",
        )
        self.client.login(username=self.student_a.email, password="pw")

        summary = self.client.get(reverse("communication:notification_summary")).json()
        target = summary["items"][0]["url"]
        self.assertIn(reverse("dashboards:student"), target)
        self.assertIn("notification_tab=announcements", target)
        self.assertIn("notification_classroom={}".format(self.vc.pk), target)

        opened = self.client.get(
            reverse("communication:notification_open", args=[notification.pk])
        )
        self.assertEqual(opened.status_code, 302)
        self.assertEqual(opened.url, target)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        dashboard = self.client.get(opened.url)
        self.assertContains(dashboard, "notification_tab")

    def test_school_announcement_opens_in_another_roles_active_dashboard(self):
        from apps.communication.models import Notification

        principal = User.objects.create_user(email="nb_principal@tass.local", password="pw")
        principal.status = "active"
        principal.save(update_fields=["status", "is_active"])
        principal.groups.add(Group.objects.get_or_create(name="principal")[0])
        announcement = SchoolAnnouncement.objects.create(
            title="School update", content="Schedule update", posted_by=self.teacher
        )
        notification = Notification.objects.create(
            recipient=principal,
            notification_type=Notification.TYPE_ANNOUNCEMENT,
            source_type="school_announcement",
            source_id=announcement.pk,
            title=announcement.title,
            message=announcement.content,
        )
        self.client.login(username=principal.email, password="pw")

        opened = self.client.get(
            reverse("communication:notification_open", args=[notification.pk])
        )
        self.assertEqual(opened.status_code, 302)
        self.assertIn(reverse("dashboards:principal"), opened.url)
        self.assertIn("notification_tab=announcements", opened.url)
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)
        dashboard = self.client.get(opened.url)
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "notifications.js")
        self.assertContains(dashboard, 'id="view-announcements"')

    def test_notifications_page_renders_with_base_header(self):
        from apps.communication import notifications as svc

        svc.notify_class_announcement(self._announcement())
        self.client.login(username=self.student_a.email, password="pw")
        resp = self.client.get(reverse("communication:notifications"))
        self.assertEqual(resp.status_code, 200)
        # base.html's bell config + the shared script must be present.
        self.assertContains(resp, "notifications.js")
        self.assertContains(resp, "tassNotifyConfig")

    def test_spa_api_endpoints(self):
        from apps.communication import notifications as svc
        from apps.communication.models import Notification

        svc.notify_class_announcement(self._announcement())
        self.client.login(username=self.student_a.email, password="pw")
        resp = self.client.get(reverse("api_notifications"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["unread_count"], 1)
        resp = self.client.post(reverse("api_notifications_mark_all_read"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            Notification.objects.filter(recipient=self.student_a, is_read=False).count(), 0
        )

    def test_open_marks_read_and_redirects(self):
        from apps.communication import notifications as svc
        from apps.communication.models import Notification

        svc.notify_class_announcement(self._announcement())
        n = Notification.objects.get(recipient=self.student_a)
        self.client.login(username=self.student_a.email, password="pw")
        resp = self.client.get(reverse("communication:notification_open", args=[n.pk]))
        self.assertEqual(resp.status_code, 302)
        n.refresh_from_db()
        self.assertTrue(n.is_read)
        self.assertIsNotNone(n.read_at)

    def test_announcement_click_lands_on_the_specific_row(self):
        from apps.communication import notifications as svc
        from apps.communication.models import Notification

        ann = self._announcement()
        svc.notify_class_announcement(ann)
        n = Notification.objects.get(recipient=self.student_a)

        # The bell advertises a route into the active student classroom UI.
        self.client.login(username=self.student_a.email, password="pw")
        data = self.client.get(reverse("communication:notification_summary")).json()
        item = data["items"][0]
        self.assertIn(reverse("dashboards:student"), item["url"])
        self.assertIn("notification_tab=announcements", item["url"])
        self.assertIn("notification_record={}".format(ann.pk), item["url"])

        # Opening the notification marks it read and returns to that SPA route.
        resp = self.client.get(reverse("communication:notification_open", args=[n.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, item["url"])
        dashboard = self.client.get(resp.url)
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "notification_classroom")

    def test_summary_exposes_in_page_target_for_announcement(self):
        from apps.communication import notifications as svc

        ann = self._announcement()
        svc.notify_class_announcement(ann)
        self.client.login(username=self.student_a.email, password="pw")
        data = self.client.get(reverse("communication:notification_summary")).json()
        item = data["items"][0]
        # The dashboards anchor their own rows by these fields, so a click can
        # open the exact announcement without a page load.
        self.assertEqual(item["source_type"], "class_announcement")
        self.assertEqual(item["source_id"], ann.pk)
        self.assertEqual(item["classroom_id"], self.vc.pk)

    def test_open_is_404_for_another_user(self):
        from apps.communication import notifications as svc
        from apps.communication.models import Notification

        svc.notify_class_announcement(self._announcement())
        n = Notification.objects.get(recipient=self.student_a)
        self.client.login(username=self.outsider.email, password="pw")
        resp = self.client.get(reverse("communication:notification_open", args=[n.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_mark_all_read_clears_only_own_bell(self):
        from apps.communication import notifications as svc
        from apps.communication.models import Notification

        svc.notify_class_announcement(self._announcement())
        self.client.login(username=self.student_a.email, password="pw")
        resp = self.client.post(reverse("communication:notification_mark_all_read"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            Notification.objects.filter(recipient=self.student_a, is_read=False).count(), 0
        )
        self.assertEqual(
            Notification.objects.filter(recipient=self.student_b, is_read=False).count(), 1
        )

    def test_assessment_assignment_notifies(self):
        from apps.assessments.models import Assessment, AssessmentAssignment
        from apps.communication import notifications as svc
        from apps.communication.models import Notification

        assessment = Assessment.objects.create(
            subject=self.subject,
            academic_year=self.year,
            created_by=self.teacher,
            title="Quiz 1",
            assessment_method=Assessment.METHOD_QUIZ,
            assessment_type=Assessment.TYPE_WRITTEN_ORAL,
            status=Assessment.STATUS_ACTIVE,
            submission_deadline=timezone.now() + datetime.timedelta(days=3),
        )
        assignment = AssessmentAssignment.objects.create(
            assessment=assessment, virtual_classroom=self.vc, assigned_by=self.teacher
        )
        self.assertEqual(svc.notify_assessment_assignment(assignment), 2)
        self.assertEqual(
            Notification.objects.filter(
                notification_type="assessment", source_id=assessment.pk
            ).count(),
            2,
        )

    def test_future_assessment_does_not_notify_until_available(self):
        from apps.assessments.models import Assessment, AssessmentAssignment
        from apps.communication import notifications as svc
        from apps.communication.models import Notification
        from django.utils import timezone

        assessment = Assessment.objects.create(
            subject=self.subject,
            academic_year=self.year,
            created_by=self.teacher,
            title="Scheduled Quiz",
            assessment_method=Assessment.METHOD_QUIZ,
            assessment_type=Assessment.TYPE_WRITTEN_ORAL,
            status=Assessment.STATUS_ACTIVE,
            submission_deadline=timezone.now() + datetime.timedelta(days=5),
            available_at=timezone.now() + datetime.timedelta(days=2),
        )
        assignment = AssessmentAssignment.objects.create(
            assessment=assessment, virtual_classroom=self.vc, assigned_by=self.teacher
        )
        self.assertEqual(svc.notify_assessment_assignment(assignment), 0)
        self.assertEqual(
            Notification.objects.filter(
                notification_type="assessment", source_id=assessment.pk
            ).count(),
            0,
        )


class MessageableContactsTests(TestCase):
    """The Personal picker offers every learner enrolled in the classroom.

    Learners are created ``pending_verification`` until they activate their
    account, so the picker (and the send path behind it) must key on enrollment
    rather than account status; otherwise a teacher cannot reach the class she
    actually runs. Archived accounts stay out.
    """

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
        self.teacher = User.objects.create_user(email="t_picker@tass.local", password="pw")
        self.teacher.status = User.Status.ACTIVE
        self.teacher.save()
        teacher_group, _ = Group.objects.get_or_create(name="teacher")
        self.teacher.groups.add(teacher_group)
        self.vc = VirtualClassroom.objects.create(
            academic_year=self.year,
            section=self.section,
            subject=self.subject,
            teacher=self.teacher,
        )
        self.pending, self.pending_profile = _student_user("s_pending", self.year, 1)
        _enroll(self.pending_profile, self.vc)
        self.archived, self.archived_profile = _student_user("s_archived", self.year, 2)
        self.archived.status = User.Status.ARCHIVED
        self.archived.save()
        _enroll(self.archived_profile, self.vc)

    def test_picker_lists_pending_learner_and_scopes_to_the_classroom(self):
        contacts, by_classroom = messageable_contacts(self.teacher)
        self.assertIn(self.pending.pk, {c["id"] for c in contacts})
        self.assertNotIn(self.archived.pk, {c["id"] for c in contacts})
        bucket = by_classroom[str(self.vc.pk)]
        self.assertIn(self.pending.pk, {c["id"] for c in bucket})

    def test_rest_can_open_and_send_to_a_pending_learner(self):
        self.client.force_login(self.teacher)
        sent = self.client.post(
            reverse("api_messages"),
            data={"recipient_id": self.pending.pk, "body": "Please catch up."},
        )
        self.assertEqual(sent.status_code, 200)
        self.assertTrue(sent.json()["ok"])
        opened = self.client.get(reverse("api_messages"), {"peer": self.pending.pk})
        self.assertEqual(opened.status_code, 200)
        self.assertTrue(opened.json()["ok"])
        self.assertNotIn(self.archived.pk, {c["id"] for c in messageable_contacts(self.teacher)[0]})
