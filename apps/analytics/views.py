import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.db.models import Avg, Count, Q
from django.http import FileResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.core.paginator import Paginator

from apps.accounts.decorators import perm_required
from apps.accounts.models import ActivityLog, StudentProfile, User
from apps.academic_structure.models import AcademicYear, Section, StudentEnrollment
from apps.assessments.models import Assessment, AssessmentAssignment, Submission
from apps.attendance.models import AttendanceRecord
from apps.virtual_classrooms.models import (
    VirtualClassroom,
    VirtualClassroomEnrollment,
)

from .forms import SFReportForm
from .models import GeneratedReport


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
    if user.is_superuser:
        return VirtualClassroom.objects.select_related(
            "academic_year", "section", "subject", "teacher"
        ).all()
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

    enrollments = StudentEnrollment.objects.filter(status=StudentEnrollment.STATUS_ENROLLED)
    sections = Section.objects.all()
    submissions = Submission.objects.all()
    attendance = AttendanceRecord.objects.all()

    if year_filter:
        enrollments = enrollments.filter(academic_year=year_filter)
        sections = sections.filter(academic_year=year_filter)
        submissions = submissions.filter(
            assessment_assignment__assessment__academic_year=year_filter
        )
        attendance = attendance.filter(
            session__virtual_classroom__academic_year=year_filter
        )

    total_students = enrollments.values("student").distinct().count()
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

    context = {
        "years": years,
        "selected_year": year_filter,
        "total_students": total_students,
        "total_sections": total_sections,
        "total_submissions": total_submissions,
        "attendance_rate": attendance_rate,
        "avg_score": _fmt_avg(avg_score),
        "attendance_stats": attendance_stats,
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
    if request.method == "POST" and form.is_valid():
        report_type = form.cleaned_data["report_type"]
        academic_year = form.cleaned_data["academic_year"]
        generated = _generate_sf_report(request.user, report_type, academic_year)
        messages.success(
            request,
            f"{generated.get_report_type_display()} generated successfully.",
        )
        return redirect("analytics:sf_report_home")

    reports = (
        GeneratedReport.objects.select_related("academic_year", "generated_by")
        .filter(report_type__in=[GeneratedReport.TYPE_SF1, GeneratedReport.TYPE_SF2])
        .order_by("-generated_at")
    )

    context = {
        "form": form,
        "structure_version": structure_version,
        "active_year": active_year,
        "reports": reports,
    }
    return render(request, "analytics/sf_report_home.html", context)


def _generate_sf_report(user, report_type, academic_year):
    students = (
        StudentEnrollment.objects.filter(
            academic_year=academic_year,
            status=StudentEnrollment.STATUS_ENROLLED,
        )
        .select_related("student", "section")
        .order_by("section__grade_level", "section__name", "student__last_name")
    )

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
                    e.section.grade_level,
                    e.section.name,
                ]
            )
        scope = f"SF1 Learner Roster - {academic_year.label}"
    else:
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
                    e.section.name,
                    stats["present"],
                    stats["late"],
                    stats["excused"],
                    stats["unexcused"],
                    total,
                ]
            )
        scope = f"SF2 Daily Attendance - {academic_year.label}"

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


def report_download(request, pk):
    report = get_object_or_404(GeneratedReport, pk=pk)
    return FileResponse(
        report.file.open("rb"),
        as_attachment=True,
        filename=report.file.name.rsplit("/", 1)[-1],
    )


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
