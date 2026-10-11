from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.campus import (
    ROLE_CAMPUS,
    can_post_to_campus,
    campus_scope_for,
    filter_school_announcements,
    scoped_classrooms,
)
from apps.accounts.decorators import has_permission, perm_required
from apps.accounts.models import User
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .access import can_message, display_name, messageable_contacts
from .forms import (
    ClassAnnouncementForm,
    GroupMessageForm,
    PrivateMessageForm,
    SchoolAnnouncementForm,
)
from .models import (
    ClassAnnouncement,
    GroupMessage,
    PrivateMessage,
    SchoolAnnouncement,
)

# Role codes whose visibility is bounded by a campus. Kept as the keys of
# ``campus.ROLE_CAMPUS`` so the two can never disagree about which roles are
# campus-bound.
CAMPUS_ADMIN_GROUPS = tuple(ROLE_CAMPUS)


def _classrooms_for_user(user):
    if user.is_superuser or user.groups.filter(name__in=("system_admin", "registrar")).exists():
        return VirtualClassroom.objects.all()
    # Campus-bound leadership -- both principals and the Assistant Principal,
    # who sits under the high school campus -- is handed only its own campus's
    # rooms. The role gate on its own would let the Elementary Principal open a
    # Grade 11 classroom's announcements and group threads.
    if user.groups.filter(name__in=CAMPUS_ADMIN_GROUPS).exists():
        return scoped_classrooms(VirtualClassroom.objects.all(), user)
    if user.groups.filter(name="teacher").exists():
        return VirtualClassroom.objects.filter(teacher=user)
    # Identify a student by the profile they actually have, not by group
    # membership. Role groups are synced by ``sync_role_permissions``, so a
    # student created (or imported) before that runs has a StudentProfile but
    # no "student" group -- and the old check then put them in no live rooms at
    # all, so classroom work silently stopped updating on their screen.
    if hasattr(user, "student_profile"):
        return VirtualClassroom.objects.filter(
            enrollments__student=user.student_profile,
            enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).distinct()
    return VirtualClassroom.objects.none()


def _user_display(user):
    # Kept as a thin wrapper so the rest of this module does not have to know
    # that the shared name helper now lives in access.py next to can_message().
    return display_name(user)


@perm_required("view_announcements_custom")
def announcement_list(request):
    type_filter = request.GET.get("type", "")
    classrooms = _classrooms_for_user(request.user)

    school_qs = filter_school_announcements(
        SchoolAnnouncement.objects.select_related("posted_by"), request.user
    )
    class_qs = (
        ClassAnnouncement.objects.select_related("virtual_classroom", "posted_by")
        .filter(virtual_classroom__in=classrooms)
    )

    allowed_type = type_filter in ("school", "class")
    show_school = not allowed_type or type_filter == "school"
    show_class = not allowed_type or type_filter == "class"

    context = {
        "school_announcements": school_qs if show_school else SchoolAnnouncement.objects.none(),
        "class_announcements": class_qs if show_class else ClassAnnouncement.objects.none(),
        "active_type": type_filter,
        "can_post_school": has_permission(request.user, "post_school_wide_announcement"),
        "can_post_class": has_permission(request.user, "post_class_announcement"),
        "can_update_school": has_permission(request.user, "update_school_wide_announcement"),
        "can_archive_school": has_permission(request.user, "archive_school_wide_announcement"),
        "can_update_class": has_permission(request.user, "update_class_announcement"),
        "can_archive_class": has_permission(request.user, "archive_class_announcement"),
    }
    return render(request, "communication/announcement_list.html", context)


@perm_required("post_school_wide_announcement")
def school_announcement_add(request):
    form = SchoolAnnouncementForm(request.POST or None, request.FILES or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        announcement = form.save(commit=False)
        announcement.posted_by = request.user
        announcement.audience_at_posted_at = campus_scope_for(request.user)
        announcement.save()
        messages.success(request, "School-wide announcement published.")
        return redirect(reverse("communication:announcements"))
    return render(
        request,
        "communication/announcement_form.html",
        {
            "form": form,
            "title": "Add School Announcement",
            "kind": "school",
            "back_url": reverse("communication:announcements"),
        },
    )


@perm_required("update_school_wide_announcement")
def school_announcement_update(request, pk):
    announcement = get_object_or_404(SchoolAnnouncement, pk=pk)
    if not can_post_to_campus(request.user, announcement.audience or "whole_school"):
        return HttpResponseForbidden("You cannot update an announcement outside your campus.")
    form = SchoolAnnouncementForm(request.POST or None, request.FILES or None, instance=announcement, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "School announcement updated.")
        return redirect(reverse("communication:announcements"))
    return render(
        request,
        "communication/announcement_form.html",
        {
            "form": form,
            "title": "Update School Announcement",
            "kind": "school",
            "announcement": announcement,
            "back_url": reverse("communication:announcements"),
        },
    )


@perm_required("archive_school_wide_announcement")
def school_announcement_archive(request, pk):
    if request.method == "POST":
        announcement = get_object_or_404(SchoolAnnouncement, pk=pk)
        if not can_post_to_campus(request.user, announcement.audience or "whole_school"):
            return HttpResponseForbidden("You cannot archive an announcement outside your campus.")
        announcement.status = SchoolAnnouncement.STATUS_ARCHIVED
        announcement.save()
        messages.success(request, "School announcement archived.")
    return redirect(reverse("communication:announcements"))


@perm_required("post_class_announcement")
def class_announcement_add(request, vc_pk=None):
    """Post to one virtual classroom.

    ``vc_pk`` is optional so the announcements index keeps a classroom picker,
    but every entry point inside a classroom passes it, which pins the post to
    that classroom instead of whichever one the teacher happens to teach.
    """
    locked = None
    if vc_pk is not None:
        locked = get_object_or_404(VirtualClassroom, pk=vc_pk)
        if locked not in _classrooms_for_user(request.user):
            return HttpResponseForbidden("You do not have permission to post in this classroom.")
    form = ClassAnnouncementForm(
        request.POST or None,
        request.FILES or None,
        user=request.user,
        instance=None,
    )
    if locked is not None:
        form.fields["virtual_classroom"].queryset = VirtualClassroom.objects.filter(pk=locked.pk)
        form.fields["virtual_classroom"].initial = locked.pk
        form.fields["virtual_classroom"].widget = forms.HiddenInput()
    if request.method == "POST" and form.is_valid():
        announcement = form.save(commit=False)
        if locked is not None:
            announcement.virtual_classroom = locked
        announcement.posted_by = request.user
        announcement.save()
        messages.success(request, "Class announcement posted.")
        if locked is not None:
            return redirect(reverse("classrooms:classroom_detail", args=[locked.pk]))
        return redirect(reverse("communication:announcements"))
    return render(
        request,
        "communication/announcement_form.html",
        {
            "form": form,
            "title": "Add Class Announcement",
            "kind": "class",
            "classroom": locked,
            "back_url": (
                reverse("classrooms:classroom_detail", args=[locked.pk])
                if locked is not None
                else reverse("communication:announcements")
            ),
        },
    )


@perm_required("update_class_announcement")
def class_announcement_update(request, pk):
    announcement = get_object_or_404(ClassAnnouncement, pk=pk)
    if announcement.virtual_classroom not in _classrooms_for_user(request.user):
        return HttpResponseForbidden("You do not have permission to edit this announcement.")
    form = ClassAnnouncementForm(request.POST or None, request.FILES or None, instance=announcement, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Class announcement updated.")
        return redirect(reverse("communication:announcements"))
    return render(
        request,
        "communication/announcement_form.html",
        {
            "form": form,
            "title": "Update Class Announcement",
            "kind": "class",
            "announcement": announcement,
            "back_url": reverse("communication:announcements"),
        },
    )


@perm_required("archive_class_announcement")
def class_announcement_archive(request, pk):
    if request.method == "POST":
        announcement = get_object_or_404(ClassAnnouncement, pk=pk)
        if announcement.virtual_classroom not in _classrooms_for_user(request.user):
            return HttpResponseForbidden("You do not have permission to archive this announcement.")
        announcement.status = ClassAnnouncement.STATUS_ARCHIVED
        announcement.save()
        messages.success(request, "Class announcement archived.")
    return redirect(reverse("communication:announcements"))


@login_required
def message_threads(request):
    if not (
        has_permission(request.user, "send_private_message")
        or has_permission(request.user, "send_group_messaging")
    ):
        return HttpResponseForbidden("You do not have permission to access this page.")
    classrooms = _classrooms_for_user(request.user)

    group_qs = (
        GroupMessage.objects.filter(virtual_classroom__in=classrooms)
        .select_related("virtual_classroom", "sender", "virtual_classroom__subject", "virtual_classroom__section")
    )

    group_data = {}
    for m in group_qs:
        entry = group_data.setdefault(
            m.virtual_classroom_id,
            {"last": m, "unread": 0, "last_seen": None},
        )
        if entry["last"] is None or m.sent_at > entry["last"].sent_at:
            entry["last"] = m

    conversations = []

    for vc in classrooms:
        data = group_data.get(vc.pk)
        if data and data["last"] is not None:
            last = data["last"]
            conversations.append((
                "group",
                f"{vc.subject.name} - {vc.section.name}",
                reverse("communication:group_message_thread", args=[vc.pk]),
                f"{_user_display(last.sender)}: {last.content}",
                None,
            ))

    sent_list = (
        PrivateMessage.objects.filter(sender=request.user)
        .values_list("recipient_id")
    )
    received_list = (
        PrivateMessage.objects.filter(recipient=request.user)
        .values_list("sender_id")
    )
    peer_ids = set()
    for sid in sent_list:
        peer_ids.add(sid[0])
    for sid in received_list:
        peer_ids.add(sid[0])

    peers = User.objects.filter(id__in=peer_ids)

    private_by_peer = {}
    for peer in peers:
        unread = PrivateMessage.objects.filter(
            sender=peer, recipient=request.user, read_at__isnull=True
        ).count()
        last = (
            PrivateMessage.objects.filter(
                Q(sender=request.user, recipient=peer) | Q(sender=peer, recipient=request.user)
            )
            .order_by("-sent_at")
            .first()
        )
        last_text = f"{_user_display(peer)}: {last.content}" if last else "No messages yet"
        conversations.append((
            "private",
            _user_display(peer),
            reverse("communication:private_message_thread", args=[peer.pk]),
            last_text,
            unread,
        ))
        private_by_peer[peer.pk] = last

    conversations.sort(key=lambda c: c[4] or 0, reverse=False)

    # People this user may start a conversation with but has never written to.
    # Without them the page could only ever list threads that already existed,
    # so a student who had never messaged their teacher had nowhere to begin:
    # their list read "No conversations yet" and stayed that way forever.
    contacts, _by_classroom = messageable_contacts(request.user)
    new_contacts = [c for c in contacts if c["id"] not in private_by_peer]

    return render(
        request,
        "communication/message_threads.html",
        {
            "conversations": conversations,
            "new_contacts": new_contacts,
        },
    )


@perm_required("send_group_messaging")
def group_message_thread(request, vc_pk):
    classroom = get_object_or_404(VirtualClassroom, pk=vc_pk)
    if classroom not in _classrooms_for_user(request.user):
        return HttpResponseForbidden("You do not have permission to access this page.")

    form = GroupMessageForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        message = form.save(commit=False)
        message.virtual_classroom = classroom
        message.sender = request.user
        message.attachment = request.FILES.get("attachment")
        message.save()
        _next = request.POST.get("next") or request.GET.get("next")
        if _next and _next.startswith("/") and not _next.startswith("//"):
            return redirect(_next)
        return redirect(reverse("communication:group_message_thread", args=[vc_pk]))

    messages_qs = (
        GroupMessage.objects.filter(virtual_classroom=classroom)
        .select_related("sender")
        .order_by("-sent_at")
    )
    return render(
        request,
        "communication/group_message_thread.html",
        {
            "classroom": classroom,
            "messages": messages_qs,
            "form": form,
        },
    )


@perm_required("send_private_message")
def private_message_thread(request, user_pk):
    peer = get_object_or_404(User, pk=user_pk)
    if peer == request.user:
        return HttpResponseForbidden("You cannot chat with yourself.")
    # The JSON endpoint and the WebSocket consumer both run can_message(), but
    # this page did not: holding `send_private_message` was enough to open a
    # thread with *any* account id in the URL. Now the HTML form and the REST
    # path enforce one rule, so the dashboard and this page cannot disagree
    # about who a student is allowed to write to.
    if not can_message(request.user, peer):
        return HttpResponseForbidden("You do not share a class with this user.")

    form = PrivateMessageForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        PrivateMessage.objects.create(
            sender=request.user,
            recipient=peer,
            content=form.cleaned_data["content"],
            attachment=request.FILES.get("attachment"),
        )
        return redirect(reverse("communication:private_message_thread", args=[peer.pk]))

    PrivateMessage.objects.filter(sender=peer, recipient=request.user, read_at__isnull=True).update(
        read_at=timezone.now()
    )

    messages_qs = (
        PrivateMessage.objects.filter(
            Q(sender=request.user, recipient=peer) | Q(sender=peer, recipient=request.user)
        )
        .select_related("sender", "recipient")
        # Oldest first, the way a conversation reads. This was `-sent_at`, which
        # painted the newest reply at the top and the opening line at the bottom.
        .order_by("sent_at")
    )
    return render(
        request,
        "communication/private_message_thread.html",
        {
            "peer": peer,
            "peer_display": _user_display(peer),
            "messages": messages_qs,
            "form": form,
        },
    )
