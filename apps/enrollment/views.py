from django.contrib import messages
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

def _notify_enrollment(classroom, actor, student_ids=None, removed=False):
    """Tell the classroom its roster changed.

    Each affected student is notified directly as well as via the classroom
    room: a student who has not reloaded since this page load has not joined
    ``class-<id>`` yet, so the room broadcast alone would miss them.
    """
    from apps.communication.realtime import notify_classroom, notify_user

    kind = ["enrollment"]
    try:
        notify_classroom(classroom.pk, kind=kind, actor=actor)
    except Exception:  # realtime must never block the save
        pass
    for sid in student_ids or []:
        try:
            notify_user(sid, kind=kind, actor=actor,
                        extra={"removed": True} if removed else None)
        except Exception:
            pass


from apps.accounts.decorators import perm_required
from apps.accounts.models import StudentProfile
from apps.accounts.ordering import order_students
from apps.virtual_classrooms.forms import VirtualClassroomEnrollForm
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .forms import EnrollmentFilterForm


@perm_required("enroll_students_to_vc")
def enrollment_dashboard(request):
    form = EnrollmentFilterForm(request.GET or None)
    if form.is_valid():
        classrooms = form.filtered_classrooms()
    else:
        classrooms = (
            VirtualClassroom.objects.select_related(
                "academic_year", "section", "subject", "teacher"
            )
            .filter(status=VirtualClassroom.STATUS_ACTIVE)
            .order_by("section__grade_level", "section__name", "subject__name")
        )

    classroom_rows = []
    for classroom in classrooms:
        classroom_rows.append(
            {
                "classroom": classroom,
                "enrolled_count": classroom.enrollments.filter(
                    status=VirtualClassroomEnrollment.STATUS_ACTIVE
                ).count(),
            }
        )

    context = {
        "form": form,
        "classroom_rows": classroom_rows,
        "total_classrooms": len(classroom_rows),
    }
    return render(request, "enrollment/dashboard.html", context)


@perm_required("enroll_students_to_vc")
def classroom_enrollment_detail(request, pk):
    classroom = get_object_or_404(
        VirtualClassroom.objects.select_related("academic_year", "section", "subject", "teacher"),
        pk=pk,
    )
    if classroom.status != VirtualClassroom.STATUS_ACTIVE:
        messages.warning(request, "This classroom is archived and cannot accept new enrollments.")
    active_enrollments = order_students(
        classroom.enrollments.select_related("student").filter(
            status=VirtualClassroomEnrollment.STATUS_ACTIVE
        ),
        prefix="student__",
    )
    context = {
        "classroom": classroom,
        "enrollments": active_enrollments,
        "enrolled_count": active_enrollments.count(),
    }
    return render(request, "enrollment/enrollment_detail.html", context)


@perm_required("enroll_students_to_vc")
def enroll_students(request, pk):
    classroom = get_object_or_404(VirtualClassroom, pk=pk)
    if classroom.status != VirtualClassroom.STATUS_ACTIVE:
        messages.error(request, "You cannot enroll students into an archived classroom.")
        return redirect(reverse("enrollment:classroom_enrollment_detail", args=[classroom.pk]))

    active_ids = classroom.enrollments.filter(
        status=VirtualClassroomEnrollment.STATUS_ACTIVE
    ).values_list("student_id", flat=True)
    form = VirtualClassroomEnrollForm(request.POST or None)
    candidate_pool = order_students(
        StudentProfile.objects.exclude(id__in=active_ids)
    )
    form.fields["students"].queryset = candidate_pool

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
            messages.success(request, f"Enrolled {created} student(s) into {classroom}.")
            _notify_enrollment(
                classroom,
                request.user,
                student_ids=[st.user_id for st in selected if st.user_id],
            )
        else:
            messages.info(request, "No new students were enrolled (some may already be enrolled).")
        return redirect(reverse("enrollment:classroom_enrollment_detail", args=[classroom.pk]))

    context = {
        "form": form,
        "classroom": classroom,
        "candidates": list(candidate_pool),
        "candidate_count": candidate_pool.count(),
    }
    return render(request, "enrollment/enroll_students.html", context)


@perm_required("archive_student_from_class")
def remove_student(request, pk, enrollment_pk):
    if request.method == "POST":
        enrollment = get_object_or_404(
            VirtualClassroomEnrollment,
            pk=enrollment_pk,
            virtual_classroom_id=pk,
        )
        enrollment.status = VirtualClassroomEnrollment.STATUS_REMOVED
        enrollment.removed_at = timezone.now()
        enrollment.save()
        messages.success(request, "Student removed from the classroom.")
        _notify_enrollment(
            classroom,
            request.user,
            student_ids=[enrollment.student.user_id] if enrollment.student else [],
            removed=True,
        )
    return redirect(reverse("enrollment:classroom_enrollment_detail", args=[pk]))