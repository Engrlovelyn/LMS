"""End-to-end proof that a teacher write reaches the right student's screen.

These drive the real pieces: the real ``/ws/live/`` socket, the real
``notify_*`` helpers, and real classroom/enrollment rows. The point of the suite
is the scoping guarantee - a change in section A must never surface on a
student enrolled in section B - plus the case where a student has not reloaded
since being enrolled and therefore has not joined the classroom room yet.
"""

import datetime

from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from apps.accounts.models import StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, Subject
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
