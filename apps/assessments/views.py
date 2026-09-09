from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.decorators import perm_required
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .forms import (
    AssessmentAssignForm,
    AssessmentForm,
    GradeScoreForm,
    SubmitForm,
)
from .models import (
    Assessment,
    AssessmentAssignment,
    AssessmentPart,
    Submission,
    SubmissionResponse,
)


ADMIN_ROLES = ("registrar", "assistant_principal", "system_admin", "principal")


def _assessments_for_user(user):
    qs = Assessment.objects.select_related("subject", "created_by", "academic_year")
    if user.is_superuser or user.groups.filter(name__in=ADMIN_ROLES).exists():
        return qs
    if user.groups.filter(name="teacher").exists():
        return qs.filter(created_by=user)
    if user.groups.filter(name="student").exists() and hasattr(user, "student_profile"):
        vc_ids = VirtualClassroomEnrollment.objects.filter(
            student=user.student_profile,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).values_list("virtual_classroom_id", flat=True)
        assignment_ids = AssessmentAssignment.objects.filter(
            virtual_classroom_id__in=vc_ids,
        ).values_list("assessment_id", flat=True)
        return qs.filter(id__in=assignment_ids).distinct()
    return qs.none()


@perm_required("view_assessment_custom")
def assessment_list(request):
    assessments = _assessments_for_user(request.user)
    status_filter = request.GET.get("status", "")
    subject_filter = request.GET.get("subject", "")
    q = request.GET.get("q", "").strip()
    if status_filter:
        assessments = assessments.filter(status=status_filter)
    if subject_filter:
        assessments = assessments.filter(subject_id=subject_filter)
    if q:
        assessments = assessments.filter(
            Q(title__icontains=q) | Q(subject__name__icontains=q)
        )
    from apps.academic_structure.models import Subject

    subjects = Subject.objects.filter(status="ACTIVE")
    context = {
        "assessments": assessments.order_by("-created_at"),
        "subjects": subjects,
        "status_filter": status_filter,
        "subject_filter": subject_filter,
        "q": q,
    }
    return render(request, "assessments/assessment_list.html", context)


@perm_required("create_assessment_custom")
def assessment_add(request):
    if not request.user.groups.filter(name="teacher").exists():
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden("Only teachers can create assessments.")
    from apps.academic_structure.models import AcademicYear

    active_year = AcademicYear.objects.filter(status="ACTIVE").first()
    if request.method == "POST" and not active_year:
        messages.error(request, "Cannot create an assessment without an active academic year.")
    form = AssessmentForm(request.POST or None)
    if form.is_valid() and active_year:
        assessment = form.save(commit=False)
        assessment.created_by = request.user
        assessment.academic_year = active_year
        assessment.save()
        messages.success(request, "Assessment created successfully.")
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    context = {"form": form, "title": "Add Assessment"}
    return render(request, "assessments/assessment_form.html", context)


@perm_required("view_assessment_custom")
def assessment_detail(request, pk):
    assessment = get_object_or_404(
        Assessment.objects.select_related("subject", "created_by", "academic_year"),
        pk=pk,
    )
    parts = assessment.parts.prefetch_related("choices").order_by("order")
    assignments = assessment.assignments.select_related(
        "virtual_classroom", "virtual_classroom__section"
    )
    is_student = request.user.groups.filter(name="student").exists()
    my_submissions = Submission.objects.none()
    if is_student and hasattr(request.user, "student_profile"):
        my_submissions = Submission.objects.filter(
            assessment_assignment__assessment=assessment,
            student=request.user.student_profile,
        ).select_related("assessment_assignment")
    submissions_count = Submission.objects.filter(
        assessment_assignment__assessment=assessment,
    ).count()
    graded_count = Submission.objects.filter(
        assessment_assignment__assessment=assessment,
        total_score__isnull=False,
    ).count()
    submissions_recent = Submission.objects.filter(
        assessment_assignment__assessment=assessment,
    ).select_related("student", "assessment_assignment__virtual_classroom").order_by(
        "-submitted_at"
    )[:5]
    context = {
        "assessment": assessment,
        "parts": parts,
        "assignments": assignments,
        "submissions_count": submissions_count,
        "graded_count": graded_count,
        "my_submissions": my_submissions,
        "submissions_recent": submissions_recent,
        "has_student_profile": hasattr(request.user, "student_profile"),
    }
    return render(request, "assessments/assessment_detail.html", context)


@perm_required("update_assessment_custom")
def assessment_update(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    form = AssessmentForm(request.POST or None, instance=assessment)
    if form.is_valid():
        form.save()
        messages.success(request, "Assessment updated successfully.")
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    context = {"form": form, "assessment": assessment, "title": "Update Assessment"}
    return render(request, "assessments/assessment_form.html", context)


@perm_required("archive_assessment_custom")
def assessment_archive(request, pk):
    if request.method == "POST":
        assessment = get_object_or_404(Assessment, pk=pk)
        assessment.status = Assessment.STATUS_ARCHIVED
        assessment.save()
        messages.success(request, "Assessment archived.")
    return redirect(reverse("assessments:assessment_list"))


@perm_required("access_assessment_repository")
def assessment_assign(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    if assessment.status == Assessment.STATUS_ARCHIVED:
        messages.error(request, "Cannot assign an archived assessment.")
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    form = AssessmentAssignForm(
        request.POST or None, assessment=assessment
    )
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data.get("assign_entire_subject"):
            selected_vcs = VirtualClassroom.objects.filter(
                status="active",
                subject=assessment.subject,
            ).exclude(
                assessment_assignments__assessment=assessment,
            )
        else:
            selected_vcs = form.cleaned_data["virtual_classrooms"]
        created_count = 0
        for vc in selected_vcs:
            existing = AssessmentAssignment.objects.filter(
                assessment=assessment,
                virtual_classroom=vc,
            ).first()
            if existing:
                continue
            is_original = not AssessmentAssignment.objects.filter(
                assessment=assessment,
                virtual_classroom__subject=assessment.subject,
            ).exists()
            AssessmentAssignment.objects.create(
                assessment=assessment,
                virtual_classroom=vc,
                assigned_by=request.user,
                is_original=is_original,
            )
            created_count += 1
        if created_count:
            messages.success(
                request, f"Assessment assigned to {created_count} classroom(s)."
            )
        else:
            messages.info(request, "No new assignments were created.")
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    context = {"form": form, "assessment": assessment}
    return render(request, "assessments/assessment_assign.html", context)


@perm_required("complete_assessment_custom")
def assessment_submit(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    if assessment.status == Assessment.STATUS_ARCHIVED:
        messages.error(request, "This assessment is no longer accepting submissions.")
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    if not hasattr(request.user, "student_profile"):
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden("Only students can submit assessments.")
    student = request.user.student_profile
    enrolled_vc_ids = VirtualClassroomEnrollment.objects.filter(
        student=student,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).values_list("virtual_classroom_id", flat=True)
    assignments = AssessmentAssignment.objects.filter(
        assessment=assessment,
        virtual_classroom_id__in=enrolled_vc_ids,
    ).select_related("virtual_classroom", "virtual_classroom__section")
    if not assignments.exists():
        messages.error(
            request, "You are not enrolled in any classroom for this assessment."
        )
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    selected_assignment = assignments.first()
    if assignments.count() > 1 and request.POST.get("assignment_id"):
        selected_assignment = get_object_or_404(
            AssessmentAssignment,
            pk=request.POST["assignment_id"],
            id__in=assignments,
        )
    existing = Submission.objects.filter(
        assessment_assignment=selected_assignment,
        student=student,
    ).first()
    if existing:
        messages.error(
            request, "You have already submitted this assessment."
        )
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    parts = assessment.parts.prefetch_related("choices").order_by("order")
    if request.method == "POST":
        form = SubmitForm(request.POST, parts=parts)
        if form.is_valid():
            submission = Submission(
                assessment_assignment=selected_assignment,
                student=student,
            )
            submission.save()
            total = Decimal("0.00")
            for part in parts:
                field_key = f"part_{part.pk}"
                answer_value = form.cleaned_data.get(field_key, "")
                response = SubmissionResponse(
                    submission=submission,
                    part=part,
                )
                if part.is_objective:
                    if part.question_type == AssessmentPart.TYPE_MULTIPLE_CHOICE:
                        selected_ids = answer_value
                        correct_ids = set(
                            part.choices.filter(is_correct=True).values_list(
                                "pk", flat=True
                            )
                        )
                        is_correct = set(int(s) for s in selected_ids) == correct_ids
                        response.response_text = ",".join(str(s) for s in selected_ids)
                    else:
                        selected_id = answer_value
                        correct = part.choices.filter(
                            pk=selected_id, is_correct=True
                        ).exists()
                        is_correct = correct
                        response.response_text = str(selected_id)
                    response.is_correct = is_correct
                    response.score = part.points if is_correct else Decimal("0.00")
                    total += response.score
                else:
                    response.response_text = answer_value
                    response.score = None
                response.save()
            submission.total_score = total
            submission.save()
            messages.success(request, "Assessment submitted successfully.")
            return redirect(
                reverse("assessments:assessment_detail", args=[assessment.pk])
            )
    else:
        form = SubmitForm(parts=parts)
    part_fields = [(part, form[f"part_{part.pk}"]) for part in parts]
    context = {
        "assessment": assessment,
        "form": form,
        "parts": parts,
        "part_fields": part_fields,
        "assignment": selected_assignment,
        "assignments": assignments,
    }
    return render(request, "assessments/assessment_submit.html", context)


@perm_required("monitor_student_submissions")
def submissions_monitor(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    submissions = Submission.objects.filter(
        assessment_assignment__assessment=assessment,
    ).select_related(
        "student",
        "assessment_assignment",
        "assessment_assignment__virtual_classroom",
    ).order_by("-submitted_at")
    context = {
        "assessment": assessment,
        "submissions": submissions,
    }
    return render(request, "assessments/submissions_monitor.html", context)


@perm_required("manually_grade_subjective_assessment")
def grade_submission(request, pk, submission_pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    submission = get_object_or_404(
        Submission,
        pk=submission_pk,
        assessment_assignment__assessment=assessment,
    )
    responses = submission.responses.select_related("part").order_by("part__order")
    subjective_responses = responses.filter(part__is_objective=False)
    if request.method == "POST":
        form = GradeScoreForm(request.POST, responses=subjective_responses)
        if form.is_valid():
            for resp in subjective_responses:
                resp.score = form.cleaned_data.get(f"score_{resp.pk}")
                resp.save()
            total = Decimal("0.00")
            for resp in responses:
                total += resp.score or Decimal("0.00")
            submission.total_score = total
            submission.graded_at = timezone.now()
            submission.teacher_remarks = form.cleaned_data.get("teacher_remarks", "")
            submission.save()
            messages.success(request, "Submission graded successfully.")
            return redirect(
                reverse("assessments:submissions_monitor", args=[assessment.pk])
            )
    else:
        form = GradeScoreForm(responses=subjective_responses)
    score_fields = [
        (resp, form[f"score_{resp.pk}"]) for resp in subjective_responses
    ]
    context = {
        "assessment": assessment,
        "submission": submission,
        "responses": responses,
        "subjective_responses": subjective_responses,
        "score_fields": score_fields,
        "form": form,
    }
    return render(request, "assessments/grade_submission.html", context)


@perm_required("access_assessment_repository")
def assessment_repository(request):
    if not request.user.groups.filter(name="teacher").exists():
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden("Only teachers can access the repository.")
    created = Assessment.objects.filter(
        created_by=request.user,
    ).select_related("subject", "academic_year").annotate(
        placement_count=Count("assignments", distinct=True)
    ).order_by("-created_at")
    context = {"assessments": created}
    return render(request, "assessments/assessment_repository.html", context)
