import csv
import io
import json
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db.models import Avg, Count, Q
from django.http import FileResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.core.paginator import Paginator
from django.views.decorators.csrf import csrf_protect

from apps.accounts.decorators import has_permission, perm_required
from apps.accounts.campus import (
    WHOLE_SCHOOL,
    assessments_for_campus,
    campus_scopes_for,
    scoped_classrooms,
    sections_for_campus,
    user_can_read_grade,
)
from apps.accounts.models import ActivityLog, StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section
from apps.assessments.models import Assessment, AssessmentAssignment, Submission
from apps.attendance.models import AttendanceRecord
from apps.virtual_classrooms.models import (
    VirtualClassroom,
    VirtualClassroomEnrollment,
)

from .forms import SFReportForm, SFReportTemplateForm
from .models import GeneratedReport, SFReportTemplate


def perm_required_any(*codenames):
    """Restrict a view to users holding at least one permission codename."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not any(has_permission(request.user, c) for c in codenames):
                return HttpResponseForbidden(
                    "You do not have permission to access this page."
                )
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def analytics_index(request):
    if request.user.has_perm("analytics.view_school_wide_analytics"):
        return redirect("analytics:school_analytics")
    if request.user.has_perm("analytics.view_class_level_analytics"):
        vc = _classrooms_for_user(request.user).first()
        if vc:
            return redirect("analytics:class_analytics", vc_pk=vc.pk)
        return render(request, "analytics/index.html", {})
    return HttpResponseForbidden("You do not have permission to access analytics.")


def _classrooms_for_user(user):
    if user.is_superuser or user.groups.filter(name="quality_audit").exists():
        return VirtualClassroom.objects.select_related(
            "academic_year", "section", "subject", "teacher"
        ).all()
    # Campus-bound principals read only their own grade band here too --
    # otherwise the class-level redirect in ``analytics_index`` hands the
    # Elementary Principal a Grade 11 classroom as their first link.
    if user.groups.filter(name__in=("principal", "elementary_principal")).exists():
        return scoped_classrooms(
            VirtualClassroom.objects.select_related(
                "academic_year", "section", "subject", "teacher"
            ).all(),
            user,
        )
    if user.groups.filter(name="teacher").exists():
        return VirtualClassroom.objects.select_related(
            "academic_year", "section", "subject", "teacher"
        ).filter(teacher=user)
    if user.groups.filter(name="student").exists() and hasattr(user, "student_profile"):
        return VirtualClassroom.objects.select_related(
            "academic_year", "section", "subject", "teacher"
        ).filter(
            enrollments__student=user.student_profile,
            enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).distinct()
    return VirtualClassroom.objects.none()


@perm_required("analytics.view_school_wide_analytics")
def school_analytics(request):
    year_pk = request.GET.get("year")
    year_filter = None
    if year_pk:
        year_filter = get_object_or_404(AcademicYear, pk=year_pk)

    years = AcademicYear.objects.all()

    enrollments = VirtualClassroomEnrollment.objects.filter(
        status=VirtualClassroomEnrollment.STATUS_ACTIVE
    )
    sections = Section.objects.all()
    submissions = Submission.objects.all()
    attendance = AttendanceRecord.objects.all()

    # A campus-scoped reader (a principal) must see the school analytics of
    # their own campus only; the numbers below are aggregated from the same
    # base querysets, so cutting them here is the whole filter.
    scopes = campus_scopes_for(request.user)
    if WHOLE_SCHOOL not in scopes:
        campus_vcs = scoped_classrooms(VirtualClassroom.objects.all(), request.user)
        enrollments = enrollments.filter(virtual_classroom__in=campus_vcs)
        sections = sections_for_campus(sections, scopes)
        submissions = submissions.filter(
            assessment_assignment__virtual_classroom__in=campus_vcs
        )
        attendance = attendance.filter(session__virtual_classroom__in=campus_vcs)

    if year_filter:
        enrollments = enrollments.filter(virtual_classroom__academic_year=year_filter)
        sections = sections.filter(academic_year=year_filter)
        submissions = submissions.filter(
            assessment_assignment__assessment__academic_year=year_filter
        )
        attendance = attendance.filter(
            session__virtual_classroom__academic_year=year_filter
        )

    total_students = enrollments.values("student_id").distinct().count()
    total_sections = sections.count()
    total_submissions = submissions.count()

    attendance_stats = attendance.aggregate(
        total=Count("id"),
        present=Count("id", filter=Q(status=AttendanceRecord.PRESENT_ON_TIME)),
        late=Count("id", filter=Q(status=AttendanceRecord.PRESENT_LATE)),
        excused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_EXCUSED)),
        unexcused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_UNEXCUSED)),
    )
    attendance_rate = _rate(
        attendance_stats.get("present", 0) + attendance_stats.get("late", 0),
        attendance_stats.get("total", 0),
    )

    avg_score = submissions.aggregate(avg=Avg("total_score"))["avg"]

    classrooms = VirtualClassroom.objects.select_related("section", "subject").filter(
        status=VirtualClassroom.STATUS_ACTIVE
    )
    if WHOLE_SCHOOL not in scopes:
        classrooms = scoped_classrooms(classrooms, request.user)
    if year_filter:
        classrooms = classrooms.filter(academic_year=year_filter)

    context = {
        "years": years,
        "selected_year": year_filter,
        "total_students": total_students,
        "total_sections": total_sections,
        "total_submissions": total_submissions,
        "attendance_rate": attendance_rate,
        "avg_score": _fmt_avg(avg_score),
        "attendance_stats": attendance_stats,
        "classrooms": classrooms,
    }
    return render(request, "analytics/school_analytics.html", context)


def _rate(numerator, denominator):
    if not denominator:
        return 0
    return round((numerator / denominator) * 100, 1)


def _fmt_avg(value):
    if value is None:
        return None
    return round(value, 2)


@perm_required("analytics.view_class_level_analytics")
def class_analytics(request, vc_pk):
    classroom = get_object_or_404(
        VirtualClassroom.objects.select_related("academic_year", "section", "subject", "teacher"),
        pk=vc_pk,
    )
    if not user_can_read_grade(request.user, classroom.section.grade_level):
        return HttpResponseForbidden("You cannot view this classroom's analytics.")

    student_ids = VirtualClassroomEnrollment.objects.filter(
        virtual_classroom=classroom,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).values_list("student_id", flat=True)

    total_students = len(student_ids)

    assignments = AssessmentAssignment.objects.filter(virtual_classroom=classroom)
    assignment_ids = assignments.values_list("id", flat=True)

    expected_submissions = total_students * assignments.count()
    submitted_count = Submission.objects.filter(
        assessment_assignment_id__in=assignment_ids
    ).count()
    submissions_rate = _rate(submitted_count, expected_submissions)

    scores = Submission.objects.filter(
        assessment_assignment_id__in=assignment_ids
    ).aggregate(avg=Avg("total_score"))["avg"]

    attendance = AttendanceRecord.objects.filter(
        student_id__in=student_ids,
        session__virtual_classroom=classroom,
    )
    attendance_stats = attendance.aggregate(
        total=Count("id"),
        present=Count("id", filter=Q(status=AttendanceRecord.PRESENT_ON_TIME)),
        late=Count("id", filter=Q(status=AttendanceRecord.PRESENT_LATE)),
        excused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_EXCUSED)),
        unexcused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_UNEXCUSED)),
    )
    present_rate = _rate(
        attendance_stats.get("present", 0) + attendance_stats.get("late", 0),
        attendance_stats.get("total", 0),
    )

    recent_logs = []
    activity_logs_qs = ActivityLog.objects.all().select_related("user")
    if student_ids:
        student_user_ids = StudentProfile.objects.filter(
            pk__in=student_ids
        ).values_list("user_id", flat=True)
        activity_logs_qs = activity_logs_qs.filter(user_id__in=student_user_ids)
    recent_logs = activity_logs_qs.order_by("-timestamp")[:10]

    context = {
        "classroom": classroom,
        "total_students": total_students,
        "submitted_count": submitted_count,
        "expected_submissions": expected_submissions,
        "submissions_rate": submissions_rate,
        "avg_score": _fmt_avg(scores),
        "present_rate": present_rate,
        "attendance_stats": attendance_stats,
        "recent_logs": recent_logs,
    }
    return render(request, "analytics/class_analytics.html", context)


@perm_required("analytics.generate_attendance_trend_analytics")
def attendance_trends(request):
    year_pk = request.GET.get("year")
    year_filter = None
    if year_pk:
        year_filter = get_object_or_404(AcademicYear, pk=year_pk)

    years = AcademicYear.objects.all()

    records = AttendanceRecord.objects.all()
    if WHOLE_SCHOOL not in campus_scopes_for(request.user):
        records = records.filter(
            session__virtual_classroom__in=scoped_classrooms(
                VirtualClassroom.objects.all(), request.user
            )
        )
    if year_filter:
        records = records.filter(session__virtual_classroom__academic_year=year_filter)

    stats = records.aggregate(
        total=Count("id"),
        present=Count("id", filter=Q(status=AttendanceRecord.PRESENT_ON_TIME)),
        late=Count("id", filter=Q(status=AttendanceRecord.PRESENT_LATE)),
        excused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_EXCUSED)),
        unexcused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_UNEXCUSED)),
    )
    attendance_rate = _rate(
        stats.get("present", 0) + stats.get("late", 0),
        stats.get("total", 0),
    )

    context = {
        "years": years,
        "selected_year": year_filter,
        "stats": stats,
        "attendance_rate": attendance_rate,
    }
    return render(request, "analytics/attendance_trends.html", context)


@perm_required("analytics.generate_submission_assessment_reports")
def submission_reports(request):
    assessment_pk = request.GET.get("assessment")
    status_filter = request.GET.get("status")

    scopes = campus_scopes_for(request.user)
    assessments = Assessment.objects.all()
    qs = (
        Submission.objects.select_related(
            "assessment_assignment__assessment",
            "assessment_assignment__virtual_classroom__section",
            "assessment_assignment__virtual_classroom__subject",
            "student",
        )
        .order_by("-submitted_at")
    )
    if WHOLE_SCHOOL not in scopes:
        campus_vcs = scoped_classrooms(VirtualClassroom.objects.all(), request.user)
        qs = qs.filter(assessment_assignment__virtual_classroom__in=campus_vcs)
        assessments = assessments_for_campus(assessments, scopes)

    if assessment_pk:
        qs = qs.filter(assessment_assignment__assessment_id=assessment_pk)
    if status_filter == "late":
        qs = qs.filter(is_late=True)
    elif status_filter == "on_time":
        qs = qs.filter(is_late=False)
    elif status_filter == "graded":
        qs = qs.filter(total_score__isnull=False)
    elif status_filter == "ungraded":
        qs = qs.filter(total_score__isnull=True)

    rows = []
    for sub in qs:
        rows.append(
            {
                "submission": sub,
                "assessment": sub.assessment_assignment.assessment,
                "subject": sub.assessment_assignment.virtual_classroom.subject,
                "section": sub.assessment_assignment.virtual_classroom.section,
                "student": sub.student,
                "status": "graded" if sub.total_score is not None else "pending",
            }
        )

    context = {
        "assessments": assessments,
        "rows": rows,
        "selected_assessment": int(assessment_pk) if assessment_pk else None,
        "selected_status": status_filter,
        "total": len(rows),
    }
    return render(request, "analytics/submission_reports.html", context)


@login_required
def sf_report_home(request):
    if not (
        request.user.has_perm("analytics.manage_sf_report_templates")
        or request.user.has_perm("analytics.generate_sf_reports")
    ):
        return HttpResponseForbidden("You do not have permission to access this page.")
    structure_version = None
    active_year = AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).first()
    if active_year and hasattr(active_year, "structure_version"):
        structure_version = active_year.structure_version

    form = SFReportForm(request.POST or None)
    if request.method == "POST":
        if "generate" in request.POST and form.is_valid():
            report_type = form.cleaned_data["report_type"]
            academic_year = form.cleaned_data["academic_year"]
            generated = _generate_sf_report(request.user, report_type, academic_year)
            messages.success(
                request,
                f"{generated.get_report_type_display()} generated successfully.",
            )
            return redirect("analytics:sf_report_home")
        if "upload_template" in request.POST:
            template_form = SFReportTemplateForm(request.POST, request.FILES)
            if template_form.is_valid():
                template = template_form.save(commit=False)
                template.uploaded_by = request.user
                template.save()
                activity_log = ActivityLog(
                    user=request.user,
                    action=f"Uploaded SF template: {template.name}",
                    related_entity_type="analytics.SFReportTemplate",
                )
                activity_log.save()
                messages.success(request, f"SF template '{template.name}' uploaded successfully.")
                return redirect("analytics:sf_report_home")
    else:
        template_form = SFReportTemplateForm()

    reports = (
        GeneratedReport.objects.select_related("academic_year", "generated_by")
        .filter(report_type__in=[GeneratedReport.TYPE_SF1, GeneratedReport.TYPE_SF2, GeneratedReport.TYPE_SF3])
        .order_by("-generated_at")
    )
    templates = SFReportTemplate.objects.select_related("uploaded_by").order_by("form_type", "name")

    context = {
        "form": form,
        "template_form": template_form,
        "structure_version": structure_version,
        "active_year": active_year,
        "reports": reports,
        "templates": templates,
    }
    return render(request, "analytics/sf_report_home.html", context)


def _generate_sf_report(user, report_type, academic_year):
    students = (
        VirtualClassroomEnrollment.objects.select_related(
            "student", "virtual_classroom__section"
        )
        .filter(
            virtual_classroom__academic_year=academic_year,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        )
        .order_by(
            "virtual_classroom__section__grade_level",
            "virtual_classroom__section__name",
            "student__last_name",
        )
    )
    students = students.distinct("student")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if report_type == GeneratedReport.TYPE_SF1:
        writer.writerow(["School Form 1 (SF1) - Learner Roster"])
        writer.writerow(["Academic Year:", academic_year.label])
        writer.writerow([])
        writer.writerow(["LRN", "Last Name", "First Name", "Middle Initial", "Suffix", "Grade", "Section"])
        for e in students:
            s = e.student
            writer.writerow(
                [
                    s.lrn,
                    s.last_name,
                    s.first_name,
                    s.middle_initial,
                    s.suffix,
                    e.virtual_classroom.section.grade_level,
                    e.virtual_classroom.section.name,
                ]
            )
        scope = f"SF1 Learner Roster - {academic_year.label}"
    elif report_type == GeneratedReport.TYPE_SF2:
        writer.writerow(["School Form 2 (SF2) - Daily Attendance"])
        writer.writerow(["Academic Year:", academic_year.label])
        writer.writerow([])
        writer.writerow(["Student", "LRN", "Section", "Present", "Late", "Excused", "Unexcused", "Total"])
        for e in students:
            s = e.student
            stats = AttendanceRecord.objects.filter(
                student=s,
                session__virtual_classroom__academic_year=academic_year,
            ).aggregate(
                present=Count("id", filter=Q(status=AttendanceRecord.PRESENT_ON_TIME)),
                late=Count("id", filter=Q(status=AttendanceRecord.PRESENT_LATE)),
                excused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_EXCUSED)),
                unexcused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_UNEXCUSED)),
            )
            total = sum(v or 0 for v in stats.values())
            writer.writerow(
                [
                    f"{s.last_name}, {s.first_name}",
                    s.lrn,
                    e.virtual_classroom.section.name,
                    stats["present"],
                    stats["late"],
                    stats["excused"],
                    stats["unexcused"],
                    total,
                ]
            )
        scope = f"SF2 Daily Attendance - {academic_year.label}"
    elif report_type == GeneratedReport.TYPE_SF3:
        writer.writerow(["School Form 3 (SF3) - Summary of Attendance"])
        writer.writerow(["Academic Year:", academic_year.label])
        writer.writerow([])
        writer.writerow(["Student", "LRN", "Section", "Present", "Late", "Excused", "Unexcused", "Total"])
        for e in students:
            s = e.student
            stats = AttendanceRecord.objects.filter(
                student=s,
                session__virtual_classroom__academic_year=academic_year,
            ).aggregate(
                present=Count("id", filter=Q(status=AttendanceRecord.PRESENT_ON_TIME)),
                late=Count("id", filter=Q(status=AttendanceRecord.PRESENT_LATE)),
                excused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_EXCUSED)),
                unexcused=Count("id", filter=Q(status=AttendanceRecord.ABSENT_UNEXCUSED)),
            )
            total = sum(v or 0 for v in stats.values())
            writer.writerow(
                [
                    f"{s.last_name}, {s.first_name}",
                    s.lrn,
                    e.virtual_classroom.section.name,
                    stats["present"],
                    stats["late"],
                    stats["excused"],
                    stats["unexcused"],
                    total,
                ]
            )
        scope = f"SF3 Summary of Attendance - {academic_year.label}"


    content = buffer.getvalue()
    filename = f"{report_type}_{academic_year.label}.csv".replace("/", "-")

    report = GeneratedReport(
        report_type=report_type,
        academic_year=academic_year,
        scope_description=scope,
        parameters={"academic_year": academic_year.label},
        generated_by=user,
    )
    report.file.save(filename, ContentFile(content.encode("utf-8")), save=True)

    activity_log = ActivityLog(
        user=user,
        action=f"Generated {scope}",
        related_entity_type="analytics.GeneratedReport",
    )
    activity_log.save()

    return report


@login_required
def report_download(request, pk):
    if not (
        has_permission(request.user, "analytics.generate_sf_reports")
        or has_permission(request.user, "analytics.manage_sf_report_templates")
    ):
        return HttpResponseForbidden("You do not have permission to download this report.")
    report = get_object_or_404(GeneratedReport, pk=pk)
    return FileResponse(
        report.file.open("rb"),
        as_attachment=True,
        filename=report.file.name.rsplit("/", 1)[-1],
    )


@perm_required_any(
    "analytics.manage_sf_report_templates", "analytics.generate_sf_reports"
)
def sf_template_download(request, pk):
    # Teachers generate SF2 directly from their dashboard and print the current
    # school form, so an uploaded template has to be reachable by everyone who
    # generates SF reports -- not only the administrator who uploaded it.
    if not (
        has_permission(request.user, "analytics.manage_sf_report_templates")
        or has_permission(request.user, "analytics.generate_sf_reports")
    ):
        return HttpResponseForbidden("You do not have permission to download this template.")
    template = get_object_or_404(SFReportTemplate, pk=pk)
    return FileResponse(
        template.template_file.open("rb"),
        as_attachment=True,
        filename=template.template_file.name.rsplit("/", 1)[-1],
    )


@login_required
@csrf_protect
@perm_required("analytics.manage_sf_report_templates")
def sf_template_upload(request):
    """Upload one SF form template (JSON response, for the dashboard).

    Multipart form fields: ``name``, ``form_type`` (default ``sf2``),
    ``description`` and ``template_file``. The template becomes the active
    override the moment it is stored and is picked up automatically by the
    teacher dashboards, so no extra activation step is required.
    """
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    data = request.POST
    name = (data.get("name") or "").strip()
    form_type = (data.get("form_type") or SFReportTemplate.FORM_SF2).strip()
    description = (data.get("description") or "").strip()
    template_file = request.FILES.get("template_file")

    if not name:
        return JsonResponse({"ok": False, "error": "Template name is required."}, status=400)
    if not template_file:
        return JsonResponse({"ok": False, "error": "Choose a template file to upload."}, status=400)

    valid_forms = {code for code, _label in SFReportTemplate.FORM_TYPE_CHOICES}
    if form_type not in valid_forms:
        return JsonResponse({"ok": False, "error": "Unknown SF form type."}, status=400)

    template = SFReportTemplate.objects.create(
        name=name,
        form_type=form_type,
        description=description,
        template_file=template_file,
        is_active=True,
        uploaded_by=request.user,
    )
    try:
        ActivityLog.objects.create(
            user=request.user,
            action=f"Uploaded SF template: {name}",
            related_entity_type="analytics.SFReportTemplate",
            related_entity_id=template.pk,
        )
    except Exception:
        pass

    return JsonResponse(
        {
            "ok": True,
            "id": template.pk,
            "name": template.name,
            "form": template.form_type,
            "active": True,
            "at": template.uploaded_at.strftime("%b %d, %Y") if template.uploaded_at else "",
            "message": f"SF template '{template.name}' uploaded. Teachers will use it automatically.",
        }
    )


@perm_required("analytics.manage_sf_report_templates")
def sf_template_delete(request, pk):
    template = get_object_or_404(SFReportTemplate, pk=pk)
    name = template.name
    template.template_file.delete(save=False)
    template.delete()
    activity_log = ActivityLog(
        user=request.user,
        action=f"Deleted SF template: {name}",
        related_entity_type="analytics.SFReportTemplate",
        related_entity_id=pk,
    )
    activity_log.save()
    messages.success(request, f"SF template '{name}' deleted.")
    return redirect("analytics:sf_report_home")


@perm_required("analytics.monitor_student_activity_logs")
def activity_logs(request):
    user_pk = request.GET.get("user")
    qs = ActivityLog.objects.select_related("user").order_by("-timestamp")

    if user_pk:
        qs = qs.filter(user_id=user_pk)

    paginator = Paginator(qs, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    users = User.objects.filter(activity_logs__isnull=False).distinct()

    context = {
        "page_obj": page_obj,
        "users": users,
        "selected_user": int(user_pk) if user_pk else None,
    }
    return render(request, "analytics/activity_logs.html", context)
