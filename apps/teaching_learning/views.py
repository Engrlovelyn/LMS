import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from html import escape
from functools import wraps
from xml.etree import ElementTree

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import ProtectedError, Q
from django.http import FileResponse, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET

from apps.academic_structure.models import AcademicYear, Subject
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .forms import LearningResourceForm, ResourceAttachForm, TopicForm
from .models import LearningResource, LearningResourcePlacement, Topic


@login_required
@xframe_options_sameorigin
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
    # Oversight roles browse the school-wide repository (see the
    # ``is_school_wide_viewer`` branch in ``apps.api.views.learning_resources``),
    # and the Academic Coordinator's copy of that list offers a preview and a
    # download for every row it shows. Re-checking them against classroom
    # membership only would deny the very action that list advertises, so the
    # read rule mirrors the listing rule instead of contradicting it.
    if user.groups.filter(
        name__in=[
            "system_admin",
            "principal",
            "elementary_principal",
            "assistant_principal",
            "quality_audit",
        ]
    ).exists() and resource.status == LearningResource.STATUS_ACTIVE:
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
        and user.groups.filter(name="teacher").exists()
        and Subject.objects.filter(
            pk__in=_teacher_subject_ids(user),
            name__iexact=resource.subject.name,
            grade_level=resource.subject.grade_level,
        ).exists()
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


def _office_converter_binary(extension=None):
    """Find a converter for this file type, allowing deployments to configure it."""
    configured = getattr(settings, "OFFICE_CONVERTER_BINARY", "")
    if configured:
        configured_binary = (
            configured if os.path.isfile(configured) else shutil.which(configured)
        )
        if configured_binary and not (
            extension == "pptx"
            and os.path.basename(configured_binary).lower() == "winword.exe"
        ):
            return configured_binary
    if sys.platform == "win32" and extension != "pptx":
        word = shutil.which("WINWORD.EXE") or shutil.which("winword")
        if word:
            return word
        program_dirs = filter(
            None,
            (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")),
        )
        for program_dir in program_dirs:
            for office_dir in ("root\\Office16", "Office16", "Office15", "Office14"):
                candidate = os.path.join(
                    program_dir, "Microsoft Office", office_dir, "WINWORD.EXE"
                )
                if os.path.isfile(candidate):
                    return candidate
    return shutil.which("soffice") or shutil.which("libreoffice")


def _convert_with_word(source_path, output_path):
    script_path = os.path.join(os.path.dirname(__file__), "office_to_pdf.ps1")
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise OSError("PowerShell is required to convert Office previews with Word.")
    return subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script_path,
            source_path,
            output_path,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
        check=False,
    )


def _office_preview_html(resource, extension):
    """Build a self-contained, escaped HTML preview for DOCX/PPTX content.

    Browsers do not render Office files directly. This keeps their document
    structure (paragraphs, headings, tables, common run formatting, and slide
    boundaries) in a sandboxed preview instead of flattening everything into
    one plain-text block. Only fixed markup and escaped document text are used.
    """
    if not resource.file or extension not in {"docx", "pptx"}:
        return ""
    try:
        with resource.file.open("rb") as source, zipfile.ZipFile(source) as archive:
            if extension == "docx":
                xml = archive.read("word/document.xml")
                if len(xml) > 10 * 1024 * 1024:
                    return ""
                root = ElementTree.fromstring(xml)
                ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                blocks = []
                body = root.find(".//" + ns + "body")
                if body is None:
                    return ""
                for node in list(body):
                    if node.tag == ns + "p":
                        props = node.find(ns + "pPr")
                        style = props.find(ns + "pStyle") if props is not None else None
                        style_id = style.get(ns + "val", "") if style is not None else ""
                        jc = props.find(ns + "jc") if props is not None else None
                        align = jc.get(ns + "val", "") if jc is not None else ""
                        runs = []
                        for run in node.findall(ns + "r"):
                            value = "".join((part.text or "") for part in run.findall(ns + "t"))
                            if not value:
                                continue
                            safe = escape(value).replace("\n", "<br>")
                            rpr = run.find(ns + "rPr")
                            if rpr is not None:
                                if rpr.find(ns + "b") is not None:
                                    safe = "<strong>" + safe + "</strong>"
                                if rpr.find(ns + "i") is not None:
                                    safe = "<em>" + safe + "</em>"
                                if rpr.find(ns + "u") is not None:
                                    safe = "<u>" + safe + "</u>"
                            runs.append(safe)
                        content = "".join(runs)
                        if not content:
                            continue
                        align_css = "text-align:%s;" % align if align in {"center", "right", "both"} else ""
                        if style_id.lower().startswith("title"):
                            blocks.append('<h1 style="%s">%s</h1>' % (align_css, content))
                        elif style_id.lower().startswith("heading"):
                            match = re.search(r"(\d+)$", style_id)
                            level = min(6, max(2, int(match.group(1)) + 1)) if match else 2
                            blocks.append('<h%d style="%s">%s</h%d>' % (level, align_css, content, level))
                        else:
                            blocks.append('<p style="%s">%s</p>' % (align_css, content))
                    elif node.tag == ns + "tbl":
                        rows = []
                        for tr in node.findall(ns + "tr"):
                            cells = []
                            for tc in tr.findall(ns + "tc"):
                                cell_text = []
                                for para in tc.findall(ns + "p"):
                                    cell_text.append(escape("".join(t.text or "" for t in para.findall(".//" + ns + "t"))))
                                cells.append("<td>%s</td>" % "<br>".join(cell_text))
                            rows.append("<tr>%s</tr>" % "".join(cells))
                        if rows:
                            blocks.append("<table>%s</table>" % "".join(rows))
                page_content = "".join(blocks)
                page_class = "document-page"
            else:
                slide_names = sorted(
                    (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                    key=lambda name: int(re.search(r"slide(\d+)", name).group(1)),
                )
                slides = []
                ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
                for number, name in enumerate(slide_names, start=1):
                    xml = archive.read(name)
                    if len(xml) > 10 * 1024 * 1024:
                        continue
                    root = ElementTree.fromstring(xml)
                    paragraphs = []
                    for para in root.findall(".//" + ns + "p"):
                        fragments = []
                        for run in list(para):
                            if run.tag not in {ns + "r", ns + "fld"}:
                                continue
                            text_node = run.find(ns + "t")
                            if text_node is None or not text_node.text:
                                continue
                            value = escape(text_node.text)
                            props = run.find(ns + "rPr")
                            if props is not None and props.get("b") in {"1", "true"}:
                                value = "<strong>" + value + "</strong>"
                            if props is not None and props.get("i") in {"1", "true"}:
                                value = "<em>" + value + "</em>"
                            fragments.append(value)
                        if fragments:
                            paragraphs.append("<p>%s</p>" % "".join(fragments))
                    if paragraphs:
                        slides.append('<section class="slide"><small>Slide %d</small>%s</section>' % (number, "".join(paragraphs)))
                page_content = "".join(slides)
                page_class = "presentation"
        if not page_content:
            return ""
        return (
            '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<style>body{margin:0;padding:24px;background:#e8edf4;color:#172033;font:16px/1.6 Arial,sans-serif}'
            '.document-page{box-sizing:border-box;max-width:850px;min-height:70vh;margin:0 auto;padding:56px 64px;background:#fff;box-shadow:0 2px 14px #0002}'
            '.document-page h1,.document-page h2,.document-page h3{line-height:1.25}.document-page table{border-collapse:collapse;width:100%%;margin:18px 0}'
            '.document-page td{border:1px solid #8792a2;padding:7px;vertical-align:top}.presentation{display:grid;gap:22px;justify-items:center}'
            '.slide{box-sizing:border-box;width:min(100%%,960px);aspect-ratio:16/9;padding:7%% 8%%;background:#fff;box-shadow:0 2px 14px #0002;overflow:auto}'
            '.slide small{display:block;color:#64748b;margin-bottom:18px}.slide p{font-size:clamp(16px,2vw,28px)}'
            '@media(max-width:600px){body{padding:8px}.document-page{padding:28px 22px}.slide{aspect-ratio:auto;min-height:360px;padding:24px}}</style>'
            '</head><body class="%s">%s</body></html>' % (page_class, page_content)
        )
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError, ValueError):
        return ""


@login_required
@require_GET
def resource_preview_page(request, pk):
    """Render a stable, refreshable URL for a student's material preview."""
    resource = get_object_or_404(
        LearningResource.objects.select_related("subject"), pk=pk, status=LearningResource.STATUS_ACTIVE
    )
    if not _can_read_resource(request.user, resource):
        return HttpResponseForbidden("You do not have access to this material.")
    return render(request, "teaching_learning/resource_preview.html", {
        "resource": resource,
        "preview_url": reverse("teaching:resource_preview", args=[resource.pk]),
    })


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
    formatted_html = _office_preview_html(resource, extension)
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
    download_url = reverse("teaching:resource_download", args=[resource.pk])
    office_pdf_url = ""
    if extension in {"docx", "pptx"} and _office_converter_binary(extension):
        office_pdf_url = reverse("teaching:resource_office_pdf", args=[resource.pk])
    return JsonResponse({
        "ok": True,
        "title": resource.title,
        "description": resource.description,
        "resource_type": resource.resource_type,
        "file_name": filename,
        "extension": extension,
        "text": text,
        "html": formatted_html,
        "office_pdf_url": office_pdf_url,
        "preview_file_url": preview_file_url if resource.file else "",
        "download_url": download_url if resource.file else "",
        "external_url": resource.external_link or "",
    })


@login_required
@require_GET
@xframe_options_sameorigin
def resource_office_pdf(request, pk):
    """Convert an authorized Office document to PDF for faithful in-browser viewing.

    Word or LibreOffice renders into a per-request temporary directory. No
    converted copy is retained in shared storage.
    """
    resource = get_object_or_404(
        LearningResource.objects.select_related("subject"),
        pk=pk,
        status=LearningResource.STATUS_ACTIVE,
    )
    if not _can_read_resource(request.user, resource):
        return HttpResponseForbidden("You do not have access to this material.")
    if not resource.file:
        return HttpResponse("No file attached to this resource.", status=404)
    filename = os.path.basename(resource.file.name)
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in {"docx", "pptx"}:
        return HttpResponse("Only Word and PowerPoint files can be converted.", status=415)
    converter = _office_converter_binary(extension)
    if not converter:
        return HttpResponse("Office preview conversion is not configured.", status=503)
    try:
        if resource.file.size > 50 * 1024 * 1024:
            return HttpResponse("This file is too large to convert for preview.", status=413)
        with tempfile.TemporaryDirectory(prefix="tass-office-preview-") as work_dir:
            source_path = os.path.join(work_dir, "material." + extension)
            output_path = os.path.join(work_dir, "material.pdf")
            with resource.file.open("rb") as source, open(source_path, "wb") as target:
                shutil.copyfileobj(source, target)
            profile_path = os.path.join(work_dir, "lo-profile")
            if os.path.basename(converter).lower() == "winword.exe":
                completed = _convert_with_word(source_path, output_path)
            else:
                command = [
                    converter,
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nolockcheck",
                    "-env:UserInstallation=" + "file:///" + profile_path.replace("\\", "/").lstrip("/"),
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    work_dir,
                    source_path,
                ]
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=45,
                    check=False,
                )
            if completed.returncode != 0 or not os.path.isfile(output_path):
                return HttpResponse("The Office file could not be converted for preview.", status=422)
            with open(output_path, "rb") as converted:
                pdf_data = converted.read(50 * 1024 * 1024 + 1)
            if len(pdf_data) > 50 * 1024 * 1024:
                return HttpResponse("The converted preview is too large.", status=413)
            if not pdf_data.startswith(b"%PDF-"):
                return HttpResponse("The Office converter returned an invalid preview.", status=422)
        response = HttpResponse(pdf_data, content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="learning-material-preview.pdf"'
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    except subprocess.TimeoutExpired:
        return HttpResponse("Office conversion took too long.", status=504)
    except (OSError, ValueError):
        return HttpResponse("Office preview is temporarily unavailable.", status=503)


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


def _teacher_or_permission_required(codename):
    """Keep teacher classroom tools usable even if an RBAC group sync is stale."""
    def decorate(view_func):
        @login_required
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            from apps.accounts.decorators import has_permission
            allowed = has_permission(request.user, codename) or request.user.groups.filter(name="teacher").exists()
            if not allowed:
                return HttpResponseForbidden("You do not have permission to access this page.")
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorate


@_teacher_or_permission_required("access_subject_level_repository")
def repository_list(request):
    q = request.GET.get("q", "").strip()
    subject_filter = request.GET.get("subject", "").strip()
    year_filter = request.GET.get("academic_year", "").strip()
    user = request.user
    user_groups = set(user.groups.values_list("name", flat=True))
    qs = LearningResource.objects.filter(
        share_to_repository=True,
        status=LearningResource.STATUS_ACTIVE,
    )
    years = AcademicYear.objects.order_by("-start_date", "-pk")
    selected_year = years.filter(pk=year_filter).first() if year_filter.isdigit() else None
    if selected_year is None:
        selected_year = years.filter(status=AcademicYear.STATUS_ACTIVE).first() or years.first()
    if selected_year:
        qs = qs.filter(academic_year=selected_year)
    is_teacher = "teacher" in user_groups and not user.is_superuser
    # The resource repository is school-year wide. Teachers can browse shared
    # materials across every subject in the selected year; classroom resources
    # and the assessment repository retain their separate scopes.
    if is_teacher:
        pass
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
    # Show the complete subject list for the selected school year, including
    # subjects that do not have a shared resource yet.
    # Archiving a school year also archives its subjects. They must remain
    # visible when browsing that year in the repository.
    subjects = Subject.objects.all()
    if selected_year:
        subjects = subjects.filter(academic_year=selected_year)
    subjects = subjects.order_by("grade_level", "name")
    subject_rows = [(subject, [resource for resource in resources if resource.subject_id == subject.pk]) for subject in subjects]
    return render(
        request,
        "teaching_learning/repository_list.html",
        {
            "resources": resources,
            "q": q,
            "subject_filter": subject_filter,
            "subjects": subjects,
            "subject_rows": subject_rows,
            "academic_years": years,
            "selected_year": selected_year,
        },
    )


@_teacher_or_permission_required("upload_learning_resource")
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
        resource.save()
        messages.success(
            request,
            "Learning resource added and shared to the subject repository."
            if resource.share_to_repository
            else "Learning resource added. It remains available only in its classroom placements until shared.",
        )
        return redirect(reverse("teaching:repository_list"))
    return render(
        request,
        "teaching_learning/resource_form.html",
        {"form": form, "title": "Upload Learning Resource"},
    )


@_teacher_or_permission_required("manage_learning_resources")
def resource_update(request, pk):
    resource = get_object_or_404(LearningResource, pk=pk)
    if request.user.groups.filter(name="teacher").exists() and resource.uploaded_by_id != request.user.pk:
        return HttpResponseForbidden("You can only update resources you uploaded.")
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


@_teacher_or_permission_required("manage_learning_resources")
def resource_archive(request, pk):
    if request.method == "POST":
        resource = get_object_or_404(LearningResource, pk=pk)
        if request.user.groups.filter(name="teacher").exists() and resource.uploaded_by_id != request.user.pk:
            return HttpResponseForbidden("You can only archive resources you uploaded.")
        resource.status = LearningResource.STATUS_ARCHIVED
        resource.save()
        messages.success(request, "Learning resource archived.")
    return redirect(reverse("teaching:repository_list"))


@_teacher_or_permission_required("view_download_learning_resource")
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


@_teacher_or_permission_required("add_topic_custom")
def topic_list(request, vc_pk):
    return redirect(reverse("teaching:class_materials", args=[vc_pk]))


@_teacher_or_permission_required("add_topic_custom")
def topic_add(request, vc_pk):
    vc = get_object_or_404(VirtualClassroom, pk=vc_pk)
    if request.user.groups.filter(name="teacher").exists() and vc.teacher_id != request.user.pk:
        return HttpResponseForbidden("You can only add topics to a classroom you teach.")
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


@_teacher_or_permission_required("add_topic_custom")
def topic_update(request, pk):
    topic = get_object_or_404(Topic, pk=pk)
    if request.user.groups.filter(name="teacher").exists() and topic.virtual_classroom.teacher_id != request.user.pk:
        return HttpResponseForbidden("You can only update topics in a classroom you teach.")
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


@_teacher_or_permission_required("add_topic_custom")
def topic_archive(request, pk):
    topic = get_object_or_404(Topic, pk=pk)
    if request.user.groups.filter(name="teacher").exists() and topic.virtual_classroom.teacher_id != request.user.pk:
        return HttpResponseForbidden("You can only remove topics in a classroom you teach.")
    vc_pk = topic.virtual_classroom_id
    if request.method == "POST":
        try:
            topic.delete()
            messages.success(request, "Topic deleted successfully.")
        except ProtectedError:
            messages.error(request, "This topic has attached resources and cannot be deleted.")
    return redirect(reverse("teaching:class_materials", args=[vc_pk]))


@_teacher_or_permission_required("upload_learning_resource")
def resource_attach(request, vc_pk):
    vc = get_object_or_404(VirtualClassroom, pk=vc_pk)
    if not (request.user.is_superuser or vc.teacher_id == request.user.pk):
        return HttpResponseForbidden("You can only add resources to a classroom you teach.")
    form = ResourceAttachForm(request.POST or None)
    form.fields["topic"].queryset = vc.topics.all()
    already = vc.resource_placements.values_list("resource_id", flat=True)
    form.fields["resources"].queryset = LearningResource.objects.filter(
        status=LearningResource.STATUS_ACTIVE, subject_id=vc.subject_id
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
