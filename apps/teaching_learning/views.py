import mimetypes
import os

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import ProtectedError, Q
from django.http import FileResponse, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.accounts.decorators import perm_required
from apps.academic_structure.models import AcademicYear, Subject
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .forms import LearningResourceForm, ResourceAttachForm, TopicForm
from .models import LearningResource, LearningResourcePlacement, Topic


@login_required
def resource_download(request, pk):
    """Serve a placed file, but only to someone entitled to that classroom.

    The file itself lives on shared storage, so the check matters: without it any
    logged-in user could read another section's material by guessing the id.
    """
    resource = get_object_or_404(LearningResource.objects.select_related("subject"), pk=pk)
    if not resource.file:
        return HttpResponse("No file attached to this resource.", status=404)

    user = request.user
    if not (user.is_superuser or user.is_staff):
        as_student = VirtualClassroom.objects.filter(
            resource_placements__resource=resource,
            enrollments__student__user=user,
            enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).exists()
        as_teacher = VirtualClassroom.objects.filter(
            resource_placements__resource=resource, teacher=user,
        ).exists()
        # A resource shared to the subject repository is also readable by a
        # teacher who handles that subject, even before it is placed in one of
        # their own classrooms. This mirrors the repository list rule.
        shared_to_subject = (
            resource.share_to_repository
            and resource.status == LearningResource.STATUS_ACTIVE
            and (
                resource.uploaded_by_id == user.pk
                or VirtualClassroom.objects.filter(
                    subject=resource.subject, teacher=user
                ).exists()
            )
        )
        if not (as_student or as_teacher or shared_to_subject):
            return HttpResponseForbidden("You do not have access to this material.")

    try:
        filename = os.path.basename(resource.file.name)
        content_type, _ = mimetypes.guess_type(filename)
        return FileResponse(
            resource.file.open("rb"),
            as_attachment=True,
            filename=filename,
            content_type=content_type or "application/octet-stream",
        )
    except FileNotFoundError:
        return HttpResponse("The stored file is missing.", status=404)


def _is_teacher_of(user, vc):
    return user.is_superuser or vc.teacher_id == user.id


def _notify_materials_changed(vc, actor):
    """Let the enrolled students repaint their Materials tab without a reload."""
    from apps.communication.realtime import notify_enrolled_students

    try:
        notify_enrolled_students(vc.pk, kind=["materials", "topics"], actor=actor, exclude=actor)
    except Exception:  # realtime must never block a save
        pass


def _teacher_subject_ids(user):
    return (
        Subject.objects.filter(virtual_classrooms__teacher_id=user.id)
        .values_list("id", flat=True)
        .distinct()
    )


@perm_required("access_subject_level_repository")
def repository_list(request):
    q = request.GET.get("q", "").strip()
    subject_filter = request.GET.get("subject", "").strip()
    qs = LearningResource.objects.filter(
        share_to_repository=True,
        status=LearningResource.STATUS_ACTIVE,
    )
    is_teacher = (
        request.user.groups.filter(name="teacher").exists()
        and not request.user.is_superuser
    )
    if is_teacher:
        qs = qs.filter(
            Q(uploaded_by_id=request.user.id)
            | Q(subject__virtual_classrooms__teacher_id=request.user.id)
        ).distinct()
    if subject_filter.isdigit():
        qs = qs.filter(subject_id=int(subject_filter))
    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(description__icontains=q)
            | Q(subject__name__icontains=q)
        )
    resources = (
        qs.select_related("subject", "uploaded_by", "academic_year")
        .order_by("-created_at")
    )
    subjects = Subject.objects.filter(status=Subject.STATUS_ACTIVE)
    if is_teacher:
        subjects = subjects.filter(id__in=_teacher_subject_ids(request.user))
    subjects = subjects.order_by("grade_level", "name")
    return render(
        request,
        "teaching_learning/repository_list.html",
        {
            "resources": resources,
            "q": q,
            "subject_filter": subject_filter,
            "subjects": subjects,
        },
    )


@perm_required("upload_learning_resource")
def resource_add(request):
    active_year = AcademicYear.objects.filter(status="ACTIVE").first()
    form = LearningResourceForm(request.POST or None, request.FILES or None)
    if request.user.groups.filter(name="teacher").exists():
        form.fields["subject"].queryset = Subject.objects.filter(
            id__in=_teacher_subject_ids(request.user)
        )
    if request.method == "POST" and form.is_valid():
        resource = form.save(commit=False)
        resource.uploaded_by = request.user
        resource.academic_year = active_year
        resource.share_to_repository = True
        resource.save()
        messages.success(request, "Learning resource added successfully.")
        return redirect(reverse("teaching:repository_list"))
    return render(
        request,
        "teaching_learning/resource_form.html",
        {"form": form, "title": "Upload Learning Resource"},
    )


@perm_required("manage_learning_resources")
def resource_update(request, pk):
    resource = get_object_or_404(LearningResource, pk=pk)
    form = LearningResourceForm(request.POST or None, request.FILES or None, instance=resource)
    if request.user.groups.filter(name="teacher").exists():
        form.fields["subject"].queryset = Subject.objects.filter(
            id__in=_teacher_subject_ids(request.user)
        )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Learning resource updated successfully.")
        return redirect(reverse("teaching:repository_list"))
    return render(
        request,
        "teaching_learning/resource_form.html",
        {"form": form, "resource": resource, "title": "Update Learning Resource"},
    )


@perm_required("manage_learning_resources")
def resource_archive(request, pk):
    if request.method == "POST":
        resource = get_object_or_404(LearningResource, pk=pk)
        resource.status = LearningResource.STATUS_ARCHIVED
        resource.save()
        messages.success(request, "Learning resource archived.")
    return redirect(reverse("teaching:repository_list"))


@perm_required("view_download_learning_resource")
def class_materials(request, vc_pk):
    vc = get_object_or_404(VirtualClassroom, pk=vc_pk)
    is_teacher = _is_teacher_of(request.user, vc)
    topics = vc.topics.all()
    placements = (
        vc.resource_placements.select_related("resource", "topic")
        .filter(resource__status=LearningResource.STATUS_ACTIVE)
        .order_by("topic__order")
    )
    context = {
        "vc": vc,
        "topics": topics,
        "placements": placements,
        "is_teacher": is_teacher,
    }
    return render(request, "teaching_learning/class_materials.html", context)


@perm_required("add_topic_custom")
def topic_list(request, vc_pk):
    return redirect(reverse("teaching:class_materials", args=[vc_pk]))


@perm_required("add_topic_custom")
def topic_add(request, vc_pk):
    vc = get_object_or_404(VirtualClassroom, pk=vc_pk)
    form = TopicForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        Topic.objects.create(virtual_classroom=vc, **form.cleaned_data)
        messages.success(request, "Topic added successfully.")
        return redirect(reverse("teaching:class_materials", args=[vc.pk]))
    return render(
        request,
        "teaching_learning/topic_form.html",
        {"form": form, "vc": vc, "title": "Add Topic"},
    )


@perm_required("add_topic_custom")
def topic_update(request, pk):
    topic = get_object_or_404(Topic, pk=pk)
    form = TopicForm(request.POST or None, instance=topic)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Topic updated successfully.")
        return redirect(reverse("teaching:class_materials", args=[topic.virtual_classroom_id]))
    return render(
        request,
        "teaching_learning/topic_form.html",
        {"form": form, "vc": topic.virtual_classroom, "title": "Update Topic"},
    )


@perm_required("add_topic_custom")
def topic_archive(request, pk):
    topic = get_object_or_404(Topic, pk=pk)
    vc_pk = topic.virtual_classroom_id
    if request.method == "POST":
        try:
            topic.delete()
            messages.success(request, "Topic deleted successfully.")
        except ProtectedError:
            messages.error(request, "This topic has attached resources and cannot be deleted.")
    return redirect(reverse("teaching:class_materials", args=[vc_pk]))


@perm_required("upload_learning_resource")
def resource_attach(request, vc_pk):
    vc = get_object_or_404(VirtualClassroom, pk=vc_pk)
    form = ResourceAttachForm(request.POST or None)
    form.fields["topic"].queryset = vc.topics.all()
    already = vc.resource_placements.values_list("resource_id", flat=True)
    form.fields["resources"].queryset = LearningResource.objects.filter(
        status=LearningResource.STATUS_ACTIVE
    ).exclude(id__in=already)
    if request.method == "POST" and form.is_valid():
        topic = form.cleaned_data["topic"]
        created = 0
        for resource in form.cleaned_data["resources"]:
            try:
                placement = LearningResourcePlacement.objects.create(
                    resource=resource,
                    virtual_classroom=vc,
                    topic=topic,
                    placed_by=request.user,
                    is_original=False,
                )
                created += 1
            except IntegrityError:
                continue
            try:
                from apps.communication import notifications as notif_service

                notif_service.notify_resource_placement(placement)
            except Exception:
                pass
        if created:
            messages.success(request, f"Attached {created} resource(s).")
            _notify_materials_changed(vc, request.user)
        else:
            messages.info(request, "No new resources were attached.")
        return redirect(reverse("teaching:class_materials", args=[vc.pk]))
    return render(
        request,
        "teaching_learning/resource_attach.html",
        {"form": form, "vc": vc, "topics": vc.topics.all()},
    )
