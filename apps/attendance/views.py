from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

def _notify_attendance(vc, actor):
    """Let the classroom's students repaint their Attendance tab live."""
    from apps.communication.realtime import notify_enrolled_students

    try:
        notify_enrolled_students(vc.pk, kind=["attendance"], actor=actor, exclude=actor)
    except Exception:  # realtime must never block a save
        pass


from apps.accounts.campus import (
    campus_scopes_for,
    scoped_classrooms,
    sections_for_campus,
    user_can_read_grade,
)
from apps.accounts.decorators import perm_required
from apps.accounts.models import StudentProfile
from apps.accounts.ordering import order_students
from apps.academic_structure.models import AcademicYear, Section
from apps.virtual_classrooms.models import (
    VirtualClassroom,
    VirtualClassroomEnrollment,
)

from .forms import AttendanceRecordForm, SessionDateForm, StatusUpdateForm
from .models import AttendanceRecord, AttendanceSession

ADMIN_ROLES = (
    "registrar",
    "assistant_principal",
    "system_admin",
    "principal",
    "elementary_principal",
    "quality_audit",
)


def _classrooms_for_user(user):
    if user.is_superuser or user.groups.filter(name__in=ADMIN_ROLES).exists():
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


def _can_access_classroom(classroom, user):
    if user.is_superuser or user.groups.filter(name__in=ADMIN_ROLES).exists():
        # Passing the role gate is not enough: a principal in ADMIN_ROLES is
        # campus-bound, so a Grade 11 room must stay closed to the Elementary
        # Principal even though the tuple says "principal".
        return user_can_read_grade(user, classroom.section.grade_level)
    if user.groups.filter(name="teacher").exists():
        return classroom.teacher_id == user.id
    if (
        user.groups.filter(name="student").exists()
        and hasattr(user, "student_profile")
        and classroom.enrollments.filter(
            student=user.student_profile,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).exists()
    ):
        return True
    return False


def _enrolled_students(classroom):
    ids = VirtualClassroomEnrollment.objects.filter(
        virtual_classroom=classroom,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).values_list("student_id", flat=True)
    return order_students(StudentProfile.objects.filter(id__in=ids))


def _session_stats(session):
    counts = {c: 0 for c, _ in AttendanceRecord.STATUS_CHOICES}
    agg = {}
    for s in session.records.values_list("status", flat=True):
        agg[s] = agg.get(s, 0) + 1
    counts.update(agg)
    return counts


def _absence_threshold():
    try:
        from apps.system_settings.models import AttendanceThresholdConfig

        return AttendanceThresholdConfig.get_solo().absence_count
    except Exception:
        return None


@perm_required("view_attendance_records")
def attendance_index(request):
    date_filter = request.GET.get("date") or None
    sessions = AttendanceSession.objects.select_related(
        "virtual_classroom__academic_year",
        "virtual_classroom__section",
        "virtual_classroom__subject",
    )
    if date_filter:
        sessions = sessions.filter(session_date=date_filter)

    qs = _classrooms_for_user(request.user)
    if qs.count() == 0:
        sessions = sessions.none()
    else:
        sessions = sessions.filter(virtual_classroom__in=qs)

    return render(
        request,
        "attendance/attendance_index.html",
        {"sessions": sessions, "date_filter": date_filter},
    )


@perm_required("view_attendance_records")
def class_sessions(request, vc_pk):
    classroom = get_object_or_404(
        VirtualClassroom.objects.select_related("academic_year", "section", "subject", "teacher"),
        pk=vc_pk,
    )
    if not _can_access_classroom(classroom, request.user):
        return HttpResponseForbidden("You do not have permission to access this page.")

    sessions = (
        AttendanceSession.objects.filter(virtual_classroom=classroom)
        .prefetch_related("records")
    )
    session_rows = []
    for session in sessions:
        session_rows.append({"session": session, "stats": _session_stats(session)})

    return render(
        request,
        "attendance/class_sessions.html",
        {"classroom": classroom, "session_rows": session_rows},
    )


@perm_required("record_class_attendance")
def record_attendance(request, vc_pk):
    classroom = get_object_or_404(VirtualClassroom, pk=vc_pk)
    if not _can_access_classroom(classroom, request.user):
        return HttpResponseForbidden("You do not have permission to access this page.")

    students = list(_enrolled_students(classroom))

    if request.method == "POST":
        session_date = request.POST.get("session_date")
        if not session_date:
            messages.error(request, "Please choose a session date.")
        else:
            session, _ = AttendanceSession.objects.get_or_create(
                virtual_classroom=classroom,
                session_date=session_date,
                defaults={"created_by": request.user},
            )
            saved = 0
            for student in students:
                prefix = f"student_{student.pk}"
                status = request.POST.get(f"{prefix}-status")
                remarks = request.POST.get(f"{prefix}-remarks", "")
                if not status or status not in dict(AttendanceRecord.STATUS_CHOICES):
                    continue
                try:
                    defaults = {
                        "status": status,
                        "remark": remarks,
                    }
                    attachment = request.FILES.get(f"{prefix}-attachment")
                    if attachment:
                        defaults["attachment"] = attachment
                    AttendanceRecord.objects.update_or_create(
                        session=session,
                        student=student,
                        defaults=defaults,
                    )
                    saved += 1
                except ValidationError as exc:
                    messages.error(
                        request,
                        f"{student}: {'; '.join(exc.messages)}",
                    )
            if saved:
                messages.success(request, f"Saved attendance for {saved} student(s) on {session_date}.")
                _notify_attendance(session.virtual_classroom, request.user)
            else:
                messages.warning(
                    request,
                    "No attendance records were saved. Please set a status for at least one student.",
                )
            return redirect(reverse("attendance:session_detail", args=[session.pk]))

    session_date = request.GET.get("date")
    records = {}
    session = None
    if session_date:
        session = AttendanceSession.objects.filter(
            virtual_classroom=classroom,
            session_date=session_date,
        ).first()
        if session:
            records = {
                r.student_id: r
                for r in session.records.select_related("student").all()
            }

    form = SessionDateForm(initial={"session_date": session_date})

    row_forms = []
    for student in students:
        record = records.get(student.pk)
        row_forms.append(
            {
                "student": student,
                "record": record,
                "status_form": AttendanceRecordForm(
                    prefix=f"student_{student.pk}",
                    initial={
                        "status": record.status if record else AttendanceRecord.PRESENT_ON_TIME,
                        "remarks": record.remark if record else "",
                    },
                ),
            }
        )

    context = {
        "classroom": classroom,
        "session": session,
        "session_date": session_date,
        "form": form,
        "students": students,
        "row_forms": row_forms,
    }
    return render(request, "attendance/record_attendance.html", context)


@perm_required("view_attendance_records")
def session_detail(request, pk):
    session = get_object_or_404(
        AttendanceSession.objects.select_related(
            "virtual_classroom__academic_year",
            "virtual_classroom__section",
            "virtual_classroom__subject",
            "created_by",
        ).prefetch_related("records__student"),
        pk=pk,
    )
    if not _can_access_classroom(session.virtual_classroom, request.user):
        return HttpResponseForbidden("You do not have permission to access this page.")

    records = list(session.records.select_related("student"))
    students_with_status = _enrolled_students(session.virtual_classroom)

    rows = []
    present = late = excused = unexcused = 0
    for student in students_with_status:
        record = next((r for r in records if r.student_id == student.pk), None)
        rows.append({"student": student, "record": record})
        if record:
            if record.status == AttendanceRecord.PRESENT_ON_TIME:
                present += 1
            elif record.status == AttendanceRecord.PRESENT_LATE:
                late += 1
            elif record.status == AttendanceRecord.ABSENT_EXCUSED:
                excused += 1
            else:
                unexcused += 1

    context = {
        "session": session,
        "rows": rows,
        "stats": {
            "present": present,
            "late": late,
            "excused": excused,
            "unexcused": unexcused,
        },
        "can_update": request.user.has_perm("attendance.update_attendance_record_custom")
        or request.user.is_superuser,
        "threshold": _absence_threshold(),
    }
    return render(request, "attendance/session_detail.html", context)


@perm_required("update_attendance_record_custom")
def session_update(request, pk):
    session = get_object_or_404(
        AttendanceSession.objects.select_related(
            "virtual_classroom__academic_year",
            "virtual_classroom__section",
            "virtual_classroom__subject",
        ),
        pk=pk,
    )
    if not _can_access_classroom(session.virtual_classroom, request.user):
        return HttpResponseForbidden("You do not have permission to access this page.")

    records = {r.student_id: r for r in session.records.select_related("student").all()}
    students = _enrolled_students(session.virtual_classroom)
    rows = []
    for student in students:
        record = records.get(student.pk)
        initial = {
            "status": (
                record.status if record else AttendanceRecord.PRESENT_ON_TIME
            ),
            "remarks": record.remark if record else "",
        }
        form = StatusUpdateForm(initial=initial)
        # The per-student form is rendered outside the table (a <form> may not
        # straddle <td> boundaries); each control points back at it via the HTML5
        # `form` attribute.
        form_id = f"rowform-{student.pk}"
        for field_name in ("status", "remarks", "attachment"):
            form.fields[field_name].widget.attrs["form"] = form_id
        rows.append(
            {
                "student": student,
                "record": record,
                "status_form": form,
                "form_id": form_id,
            }
        )

    return render(
        request,
        "attendance/session_update.html",
        {"session": session, "rows": rows},
    )


@perm_required("update_attendance_record_custom")
def record_status_update(request, pk):
    if request.method == "POST":
        session = get_object_or_404(AttendanceSession, pk=pk)
        if not _can_access_classroom(session.virtual_classroom, request.user):
            return HttpResponseForbidden("You do not have permission to access this page.")
        student_id = request.POST.get("student_id")
        status = request.POST.get("status")
        remarks = request.POST.get("remarks", "")
        if student_id and status in dict(AttendanceRecord.STATUS_CHOICES):
            if not _enrolled_students(session.virtual_classroom).filter(pk=student_id).exists():
                messages.error(request, "That student is not part of this class.")
            else:
                try:
                    defaults = {"status": status, "remark": remarks}
                    attachment = request.FILES.get("attachment")
                    if attachment:
                        defaults["attachment"] = attachment
                    record, _ = AttendanceRecord.objects.update_or_create(
                        session=session,
                        student_id=student_id,
                        defaults=defaults,
                    )
                    messages.success(request, f"Attendance for {record.student} updated.")
                    _notify_attendance(session.virtual_classroom, request.user)
                except ValidationError as exc:
                    messages.error(request, "; ".join(exc.messages))
        else:
            messages.error(request, "Please provide a valid status for the student.")
    return HttpResponseRedirect(reverse("attendance:session_detail", args=[pk]))


@perm_required("generate_attendance_report")
def attendance_report(request):
    years = AcademicYear.objects.all()
    scopes = campus_scopes_for(request.user)
    sections = sections_for_campus(Section.objects.all(), scopes)
    vcs = scoped_classrooms(
        VirtualClassroom.objects.select_related("academic_year", "section", "subject").all(),
        request.user,
    )

    context = {
        "years": years,
        "sections": sections,
        "vcs": vcs,
    }

    if request.method == "GET" and request.GET:
        vc_id = request.GET.get("vc")
        year_id = request.GET.get("academic_year")
        section_id = request.GET.get("section")

        qs = vcs
        if vc_id:
            qs = qs.filter(pk=vc_id)
        if year_id:
            qs = qs.filter(academic_year_id=year_id)
        if section_id:
            qs = qs.filter(section_id=section_id)

        selected = list(qs)
        if selected:
            session_ids = AttendanceSession.objects.filter(
                virtual_classroom__in=selected
            ).values_list("pk", flat=True)
            records = AttendanceRecord.objects.filter(session_id__in=session_ids)

            summary = {}
            for rec in records.select_related("student"):
                sid = rec.student_id
                row = summary.setdefault(
                    sid,
                    {
                        "student": rec.student,
                        "present": 0,
                        "late": 0,
                        "excused": 0,
                        "unexcused": 0,
                        "total": 0,
                    },
                )
                if rec.status == AttendanceRecord.PRESENT_ON_TIME:
                    row["present"] += 1
                elif rec.status == AttendanceRecord.PRESENT_LATE:
                    row["late"] += 1
                elif rec.status == AttendanceRecord.ABSENT_EXCUSED:
                    row["excused"] += 1
                else:
                    row["unexcused"] += 1
                row["total"] += 1

            rows = sorted(summary.values(), key=lambda r: r["student"].last_name)
            for row in rows:
                present_total = row["present"] + row["late"]
                row["attended"] = present_total
                row["pct"] = (
                    round(present_total * 100 / row["total"], 1) if row["total"] else 0
                )
            context["report_rows"] = rows
            context["selected_years"] = list(qs.values_list("academic_year_id", flat=True))
            context["selected_sections"] = list(qs.values_list("section_id", flat=True))

    return render(request, "attendance/attendance_report.html", context)
