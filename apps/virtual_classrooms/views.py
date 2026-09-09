from django.contrib import messages
from django.db import IntegrityError
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.decorators import perm_required, role_required
from apps.accounts.models import StudentProfile

from apps.assessments.models import Assessment, Submission
from apps.attendance.models import AttendanceRecord, AttendanceSession
from apps.communication.models import ClassAnnouncement
from apps.teaching_learning.models import LearningResource

from .forms import AddStudentForm, VirtualClassroomEnrollForm, VirtualClassroomForm
from .models import VirtualClassroom, VirtualClassroomEnrollment

ADMIN_ROLES = ("registrar", "assistant_principal", "system_admin")


def _classrooms_for_user(user):
    if user.is_superuser:
        return VirtualClassroom.objects.all()
    if user.groups.filter(name__in=ADMIN_ROLES).exists():
        return VirtualClassroom.objects.all()
    if user.groups.filter(name="teacher").exists():
        return VirtualClassroom.objects.filter(teacher=user)
    if user.groups.filter(name="student").exists() and hasattr(user, "student_profile"):
        return VirtualClassroom.objects.filter(
            enrollments__student=user.student_profile,
            enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).distinct()
    return VirtualClassroom.objects.none()


def _can_access_classroom(classroom, user):
    if user.is_superuser:
        return True
    if user.groups.filter(name__in=ADMIN_ROLES).exists():
        return True
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


def _can_teach_classroom(classroom, user):
    if user.is_superuser:
        return True
    return classroom.teacher_id == user.id


def _session_stats(session):
    records = list(session.records.all())
    counts = {"present": 0, "late": 0, "absent": 0, "excused": 0, "total": len(records)}
    for r in records:
        if r.status == AttendanceRecord.PRESENT_ON_TIME:
            counts["present"] += 1
        elif r.status == AttendanceRecord.PRESENT_LATE:
            counts["late"] += 1
        elif r.status == AttendanceRecord.ABSENT_EXCUSED:
            counts["excused"] += 1
        elif r.status == AttendanceRecord.ABSENT_UNEXCUSED:
            counts["absent"] += 1
    return counts


@perm_required("view_virtual_classroom")
def classroom_list(request):
    classrooms = _classrooms_for_user(request.user)
    return render(request, "virtual_classrooms/classroom_list.html", {"classrooms": classrooms})


@role_required("registrar", "assistant_principal")
@perm_required("create_virtual_classroom")
def classroom_add(request):
    form = VirtualClassroomForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            classroom = form.save()
        except IntegrityError:
            messages.error(request, "A classroom for this subject, section, year and grading period already exists.")
            return render(
                request,
                "virtual_classrooms/classroom_form.html",
                {"form": form, "title": "Add Virtual Classroom"},
            )
        messages.success(request, "Virtual classroom created successfully.")
        return redirect(reverse("classrooms:classroom_detail", args=[classroom.pk]))
    return render(
        request,
        "virtual_classrooms/classroom_form.html",
        {"form": form, "title": "Add Virtual Classroom"},
    )


@perm_required("view_virtual_classroom")
def classroom_detail(request, pk):
    classroom = get_object_or_404(VirtualClassroom, pk=pk)
    if not _can_access_classroom(classroom, request.user):
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden("You do not have permission to access this page.")
    enrollments = (
        classroom.enrollments.select_related("student")
        .filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE)
        .order_by("student__last_name")
    )
    is_teacher = _can_teach_classroom(classroom, request.user)

    topics = (
        classroom.topics.prefetch_related("resource_placements__resource")
        .order_by("order", "id")
    )
    topic_rows = []
    for topic in topics:
        topic_rows.append(
            {
                "topic": topic,
                "placements": topic.resource_placements.select_related("resource")
                .filter(resource__status=LearningResource.STATUS_ACTIVE)
                .order_by("-resource__created_at"),
            }
        )

    assessments = (
        classroom.assessment_assignments.select_related("assessment")
        .filter(assessment__status=Assessment.STATUS_ACTIVE)
        .order_by("-assessment__submission_deadline")
    )
    assessment_rows = []
    for aa in assessments:
        a = aa.assessment
        submitted = aa.submissions.count()
        submissions = (
            aa.submissions.select_related("student")
            .order_by("-submitted_at")[:8]
        )
        assessment_rows.append(
            {
                "assignment": aa,
                "assessment": a,
                "submitted": submitted,
                "total": enrollments.count(),
                "submissions": submissions,
            }
        )

    # Per-student performance: avg % across this class's graded submissions
    assessment_max = {}
    for aa in assessments:
        total = (
            aa.assessment.parts.aggregate(total=Sum("points"))["total"]
            or 0
        )
        assessment_max[aa.assessment_id] = float(total)
    student_rows = []
    for e in enrollments:
        subs = Submission.objects.filter(
            assessment_assignment__virtual_classroom=classroom,
            student=e.student,
            total_score__isnull=False,
        )
        ssum = 0
        smax = 0
        for s in subs:
            mx = assessment_max.get(s.assessment_assignment.assessment_id, 0)
            if mx and s.total_score is not None:
                ssum += float(s.total_score)
                smax += mx
        avg_pct = round(ssum / smax * 100) if smax else 0
        att_rows = AttendanceRecord.objects.filter(
            session__virtual_classroom=classroom,
            student=e.student,
        )
        present = att_rows.filter(status__in=[AttendanceRecord.PRESENT_ON_TIME, AttendanceRecord.PRESENT_LATE]).count()
        att_total = att_rows.count()
        att_pct = round(present / att_total * 100) if att_total else 0
        student_rows.append(
            {
                "student": e.student,
                "assessments": subs.count(),
                "avg_pct": avg_pct,
                "att_pct": att_pct,
            }
        )

    sessions = (
        AttendanceSession.objects.filter(virtual_classroom=classroom)
        .prefetch_related("records")
        .order_by("-session_date")
    )
    session_rows = []
    for session in sessions:
        session_rows.append({"session": session, "stats": _session_stats(session)})

    announcements = classroom.announcements.filter(
        status=ClassAnnouncement.STATUS_ACTIVE
    ).order_by("-posted_at")

    try:
        group_messages = (
            classroom.group_messages.select_related("sender")
            .order_by("sent_at")[:50]
        )
    except Exception:
        group_messages = []

    context = {
        "classroom": classroom,
        "now": timezone.now(),
        "is_teacher": is_teacher,
        "is_student": request.user.groups.filter(name="student").exists() and not is_teacher,
        "enrollments": enrollments,
        "students_count": enrollments.count(),
        "topics_count": classroom.topics.count(),
        "materials_count": classroom.resource_placements.count(),
        "assessments_count": assessments.count(),
        "topic_rows": topic_rows,
        "assessment_rows": assessment_rows,
        "student_rows": student_rows,
        "session_rows": session_rows,
        "announcements": announcements,
        "group_messages": group_messages,
    }
    return render(request, "virtual_classrooms/classroom_detail.html", context)


@perm_required("update_virtual_classroom")
def classroom_update(request, pk):
    classroom = get_object_or_404(VirtualClassroom, pk=pk)
    form = VirtualClassroomForm(request.POST or None, request.FILES or None, instance=classroom)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except IntegrityError:
            messages.error(request, "A classroom for this subject, section, year and grading period already exists.")
            return render(
                request,
                "virtual_classrooms/classroom_form.html",
                {"form": form, "classroom": classroom, "title": "Update Virtual Classroom"},
            )
        messages.success(request, "Virtual classroom updated successfully.")
        return redirect(reverse("classrooms:classroom_detail", args=[classroom.pk]))
    return render(
        request,
        "virtual_classrooms/classroom_form.html",
        {"form": form, "classroom": classroom, "title": "Update Virtual Classroom"},
    )


@perm_required("archive_virtual_classroom")
def classroom_archive(request, pk):
    if request.method == "POST":
        classroom = get_object_or_404(VirtualClassroom, pk=pk)
        classroom.status = VirtualClassroom.STATUS_ARCHIVED
        classroom.save()
        messages.success(request, "Virtual classroom archived.")
    return redirect(reverse("classrooms:classroom_list"))


@perm_required("enroll_students_to_vc")
def classroom_enroll(request, pk):
    classroom = get_object_or_404(VirtualClassroom, pk=pk)
    form = VirtualClassroomEnrollForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        selected = form.cleaned_data["students"]
        created = 0
        for student in selected:
            try:
                VirtualClassroomEnrollment.objects.create(
                    virtual_classroom=classroom,
                    student=student,
                )
                created += 1
            except IntegrityError:
                continue
        if created:
            messages.success(request, f"Enrolled {created} student(s).")
        else:
            messages.info(request, "No new students were enrolled (some may already be enrolled).")
        return redirect(reverse("classrooms:classroom_detail", args=[classroom.pk]))
    active_ids = classroom.enrollments.filter(
        status=VirtualClassroomEnrollment.STATUS_ACTIVE
    ).values_list("student_id", flat=True)
    form.fields["students"].queryset = form.fields["students"].queryset.exclude(
        id__in=active_ids
    )
    return render(
        request,
        "virtual_classrooms/enroll.html",
        {"form": form, "classroom": classroom},
    )


@perm_required("add_student_to_class")
def classroom_add_student(request, pk):
    classroom = get_object_or_404(VirtualClassroom, pk=pk)
    active_ids = classroom.enrollments.filter(
        status=VirtualClassroomEnrollment.STATUS_ACTIVE
    ).values_list("student_id", flat=True)
    form = AddStudentForm(request.POST or None)
    form.fields["student"].queryset = StudentProfile.objects.order_by("lrn").exclude(
        id__in=active_ids
    )
    if request.method == "POST" and form.is_valid():
        try:
            VirtualClassroomEnrollment.objects.create(
                virtual_classroom=classroom,
                student=form.cleaned_data["student"],
            )
            messages.success(request, "Student added to the classroom.")
        except IntegrityError:
            messages.error(request, "This student is already enrolled in the classroom.")
        return redirect(reverse("classrooms:classroom_detail", args=[classroom.pk]))
    return render(
        request,
        "virtual_classrooms/add_student.html",
        {"form": form, "classroom": classroom},
    )


@perm_required("archive_student_from_class")
def classroom_remove_student(request, pk, enrollment_pk):
    if request.method == "POST":
        enrollment = get_object_or_404(
            VirtualClassroomEnrollment,
            pk=enrollment_pk,
            virtual_classroom_id=pk,
        )
        enrollment.status = VirtualClassroomEnrollment.STATUS_REMOVED
        from django.utils import timezone

        enrollment.removed_at = timezone.now()
        enrollment.save()
        messages.success(request, "Student removed from the classroom.")
    return redirect(reverse("classrooms:classroom_detail", args=[pk]))

