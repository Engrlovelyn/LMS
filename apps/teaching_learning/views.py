import mimetypes
import os
import re
import zipfile
from xml.etree import ElementTree

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import ProtectedError, Q
from django.http import FileResponse, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

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
    resource = get_object_or_404(
        LearningResource.objects.select_related("subject"), pk=pk, status=LearningResource.STATUS_ACTIVE
    )
    if not resource.file:
        return HttpResponse("No file attached to this resource.", status=404)

    if not _can_read_resource(request.user, resource):
        return HttpResponseForbidden("You do not have access to this material.")

    try:
        filename = os.path.basename(resource.file.name)
        content_type, _ = mimetypes.guess_type(filename)
        return FileResponse(
            resource.file.open("rb"),
            as_attachment=request.GET.get("inline") != "1",
            filename=filename,
            content_type=content_type or "application/octet-stream",
        )
    except FileNotFoundError:
        return HttpResponse("The stored file is missing.", status=404)


def _can_read_resource(user, resource):
    if user.is_superuser or user.is_staff:
        return True
    as_student = VirtualClassroom.objects.filter(
        resource_placements__resource=resource,
        enrollments__student__user=user,
        enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).exists()
    as_teacher = VirtualClassroom.objects.filter(
        resource_placements__resource=resource, teacher=user,
    ).exists()
    shared_to_subject = (
        resource.share_to_repository
        and resource.status == LearningResource.STATUS_ACTIVE
        and (
            resource.uploaded_by_id == user.pk
            or VirtualClassroom.objects.filter(subject=resource.subject, teacher=user).exists()
        )
    )
    return as_student or as_teacher or shared_to_subject


def _office_preview_text(resource, extension):
    """Extract readable paragraph/slide text from OOXML files without downloading."""
    if not resource.file or extension not in {"docx", "pptx"}:
        return ""
    try:
        with resource.file.open("rb") as source, zipfile.ZipFile(source) as archive:
            if extension == "docx":
                xml = archive.read("word/document.xml")
                if len(xml) > 10 * 1024 * 1024:
                    return ""
                root = ElementTree.fromstring(xml)
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                paragraphs = []
                for paragraph in root.findall(".//w:p", ns):
                    text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns))
                    if text.strip():
                        paragraphs.append(text)
                return "\n\n".join(paragraphs)

            slide_names = [
                name for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ]
            slide_names.sort(key=lambda name: int(re.search(r"slide(\d+)", name).group(1)))
            slides = []
            ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
            for index, name in enumerate(slide_names, start=1):
                xml = archive.read(name)
                if len(xml) > 10 * 1024 * 1024:
                    continue
                root = ElementTree.fromstring(xml)
                paragraphs = []
                for paragraph in root.findall(".//a:p", ns):
                    text = "".join(node.text or "" for node in paragraph.findall(".//a:t", ns))
                    if text.strip():
                        paragraphs.append(text)
                if paragraphs:
                    slides.append("Slide {}\n{}".format(index, "\n".join(paragraphs)))
            return "\n\n".join(slides)
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError, ValueError):
        return ""


@login_required
@require_GET
def resource_preview(request, pk):
    """Return preview metadata/text after the same classroom access check as downloads."""
    resource = get_object_or_404(
        LearningResource.objects.select_related("subject"), pk=pk, status=LearningResource.STATUS_ACTIVE
    )
    if not _can_read_resource(request.user, resource):
        return HttpResponseForbidden("You do not have access to this material.")
    filename = os.path.basename(resource.file.name) if resource.file else ""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    text = _office_preview_text(resource, extension)
    if extension in {"txt", "md", "csv"} and resource.file:
        try:
            with resource.file.open("rb") as source:
                raw = source.read(2 * 1024 * 1024 + 1)
            if len(raw) <= 2 * 1024 * 1024:
                text = raw.decode("utf-8-sig", errors="replace")
        except OSError:
            text = ""
    text = text[:1_000_000]
    preview_file_url = reverse("teaching:resource_download", args=[resource.pk]) + "?inline=1"
    return JsonResponse({
        "ok": True,
        "title": resource.title,
        "description": resource.description,
        "resource_type": resource.resource_type,
        "file_name": filename,
        "extension": extension,
        "text": text,
        "preview_file_url": preview_file_url if resource.file else "",
        "external_url": resource.external_link or "",
    })


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
    user = request.user
    user_groups = set(user.groups.values_list("name", flat=True))
    qs = LearningResource.objects.filter(
        share_to_repository=True,
        status=LearningResource.STATUS_ACTIVE,
    )
    is_teacher = (
        user_groups.filter("teacher").exists() if False else ("teacher" in user_groups)
        and not user.is_superuser
    )
    # More robust check
    is_teacher_only = ("teacher" in user_groups and not user.is_superuser and not (user_groups & {"assistant_principal", "principal", "elementary_principal", "system_admin"}))
    if is_teacher_only or ("teacher" in user_groups and not user.is_superuser):
        qs = qs.filter(
            Q(uploaded_by_id=user.id)
            | Q(subject__virtual_classrooms__teacher_id=user.id)
        ).distinct()
    elif user.is_superuser or (user_groups & {"assistant_principal", "principal", "elementary_principal", "system_admin", "quality_audit"}):
        pass
    else:
        # fallback to teacher-subject scope
        try:
            qs = qs.filter(
                Q(uploaded_by_id=user.id)
                | Q(subject__virtual_classrooms__teacher_id=user.id)
            ).distinct()
        except Exception:
            pass

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
        .order_by("subject__grade_level", "subject__name", "title", "pk")
    )
    subjects = Subject.objects.filter(status=Subject.STATUS_ACTIVE)
    if ("teacher" in user_groups and not user.is_superuser and not (user_groups & {"assistant_principal", "principal", "elementary_principal", "system_admin"})):
        subjects = subjects.filter(id__in=_teacher_subject_ids(user))
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
    is_enrolled = VirtualClassroomEnrollment.objects.filter(
        virtual_classroom=vc,
        student__user=request.user,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).exists()
    if not (request.user.is_superuser or vc.teacher_id == request.user.pk or is_enrolled):
        from apps.virtual_classrooms.views import _classrooms_for_user

        if vc not in _classrooms_for_user(request.user):
            return HttpResponseForbidden("You do not have access to this classroom's materials.")
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
