from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.decorators import has_permission, perm_required
from apps.attendance.models import AttendanceRecord
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .forms import (
    AssessmentAssignForm,
    AssessmentForm,
    AssessmentPartForm,
    GradeScoreForm,
    GradingComponentForm,
    SubmitForm,
)
from .models import (
    Assessment,
    AssessmentAssignment,
    AssessmentPart,
    GradingComponent,
    GradingFormula,
    StudentGrade,
    Submission,
    SubmissionResponse,
)
from .services import trigger_missed_notifications


ADMIN_ROLES = ("registrar", "assistant_principal", "system_admin", "principal", "tass_administrator")


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


def _can_edit_assessment(user, assessment):
    if user.is_superuser or user.groups.filter(name__in=ADMIN_ROLES).exists():
        return True
    return assessment.created_by_id == user.id


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
        assessment.lifecycle_stage = Assessment.STAGE_AVAILABLE if (
            assessment.available_at and assessment.available_at <= timezone.now()
        ) else Assessment.STAGE_CREATED
        assessment.save()
        messages.success(request, "Assessment created successfully. Add parts to define its items.")
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
    parts = assessment.parts.order_by("order")
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
        "can_edit": _can_edit_assessment(request.user, assessment),
        "total_possible": assessment.total_possible_score,
    }
    return render(request, "assessments/assessment_detail.html", context)


@perm_required("update_assessment_custom")
def assessment_update(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    if not _can_edit_assessment(request.user, assessment):
        return HttpResponseForbidden("You cannot update this assessment.")
    form = AssessmentForm(request.POST or None, instance=assessment)
    if form.is_valid():
        updated = form.save()
        if updated.available_at and updated.available_at <= timezone.now():
            updated.lifecycle_stage = Assessment.STAGE_AVAILABLE
            updated.save(update_fields=["lifecycle_stage"])
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
        assessment.save(update_fields=["status"])
        messages.success(request, "Assessment archived.")
    return redirect(reverse("assessments:assessment_list"))


@perm_required("trigger_missed_assessment_notification")
def trigger_notifications(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    if request.method == "POST":
        summary = trigger_missed_notifications(assessment)
        if summary["created"]:
            messages.success(
                request,
                f"Created {summary['created']} missed-assessment notification(s) for {assessment.title}.",
            )
        else:
            messages.info(
                request,
                "No new missed-assessment notifications to create for this assessment.",
            )
    return redirect(reverse("assessments:assessment_detail", kwargs={"pk": pk}))


# ---------------------------------------------------------------------------
# Assessment parts (compiled item banks)
# ---------------------------------------------------------------------------
@perm_required("update_assessment_custom")
def assessment_part_add(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    if not _can_edit_assessment(request.user, assessment):
        return HttpResponseForbidden("You cannot edit this assessment.")
    if request.method == "POST":
        form = AssessmentPartForm(request.POST)
        if form.is_valid():
            part = AssessmentPart(
                assessment=assessment,
                question_text=form.cleaned_data["question_text"],
                question_type=form.cleaned_data["question_type"],
                order=form.cleaned_data.get("order") or (assessment.parts.count() + 1),
                items=form.cleaned_data["items"],
            )
            part.save()
            messages.success(request, "Part added successfully.")
            return redirect(reverse("assessments:assessment_detail", args=[assessment.pk]))
    else:
        form = AssessmentPartForm()
    context = {"form": form, "assessment": assessment, "title": "Add Part"}
    return render(request, "assessments/assessment_part_form.html", context)


@perm_required("update_assessment_custom")
def assessment_part_update(request, pk, part_pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    part = get_object_or_404(AssessmentPart, pk=part_pk, assessment=assessment)
    if not _can_edit_assessment(request.user, assessment):
        return HttpResponseForbidden("You cannot edit this assessment.")
    if request.method == "POST":
        form = AssessmentPartForm(request.POST, part=part)
        if form.is_valid():
            part.question_text = form.cleaned_data["question_text"]
            part.question_type = form.cleaned_data["question_type"]
            part.order = form.cleaned_data.get("order") or part.order
            part.items = form.cleaned_data["items"]
            part.save()
            messages.success(request, "Part updated successfully.")
            return redirect(reverse("assessments:assessment_detail", args=[assessment.pk]))
    else:
        form = AssessmentPartForm(part=part)
    context = {"form": form, "assessment": assessment, "title": "Update Part"}
    return render(request, "assessments/assessment_part_form.html", context)


@perm_required("update_assessment_custom")
def assessment_part_delete(request, pk, part_pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    part = get_object_or_404(AssessmentPart, pk=part_pk, assessment=assessment)
    if request.method == "POST":
        part.delete()
        messages.success(request, "Part deleted.")
    return redirect(reverse("assessments:assessment_detail", args=[assessment.pk]))


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
    if assessment.available_at and assessment.available_at > timezone.now():
        messages.error(request, "This assessment is not yet available for submission.")
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    if not hasattr(request.user, "student_profile"):
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
    parts = list(assessment.parts.order_by("order"))
    if request.method == "POST":
        form = SubmitForm(request.POST, parts=parts)
        if form.is_valid():
            submission = Submission(
                assessment_assignment=selected_assignment,
                student=student,
            )
            submission.save()
            for part in parts:
                response = SubmissionResponse(
                    submission=submission,
                    part=part,
                    response_data=form.response_data_for(part),
                )
                if part.is_objective:
                    response.compute_auto_score()
                    response.save()
                else:
                    response.item_scores = {}
                    response.save()
            submission.recompute_total_score()
            messages.success(request, "Assessment submitted successfully.")
            return redirect(
                reverse("assessments:assessment_detail", args=[assessment.pk])
            )
    else:
        form = SubmitForm(parts=parts)
    item_groups = []
    for part in parts:
        entries = []
        for item_key, fname in form.answers.get(part.pk, {}).items():
            if isinstance(fname, list):
                entries.append((item_key, [form[f] for f in fname], True))
            else:
                entries.append((item_key, form[fname], False))
        item_groups.append((part, entries))
    context = {
        "assessment": assessment,
        "form": form,
        "item_groups": item_groups,
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
    responses = list(submission.responses.select_related("part").order_by("part__order"))
    subjective_responses = [r for r in responses if r.part.requires_manual_grading]
    if request.method == "POST":
        form = GradeScoreForm(request.POST, responses=subjective_responses)
        if form.is_valid():
            scores_map = form.build_item_scores()
            for resp in subjective_responses:
                per_item = scores_map.get(resp.pk, {})
                resp.item_scores = {
                    item_id: {
                        "score": per_item[item_id]["score"],
                        "feedback": per_item[item_id]["feedback"],
                    }
                    for item_id in resp.part.items
                    if item_id in per_item
                }
                resp.score = Decimal(
                    str(sum((v["score"] for v in resp.item_scores.values()), 0))
                ) if resp.item_scores else None
                resp.grading_status = SubmissionResponse.GRADED
                resp.graded_by = request.user
                resp.graded_at = timezone.now()
                resp.save(update_fields=["item_scores", "score", "grading_status", "graded_by", "graded_at"])
            submission.recompute_total_score()
            submission.graded_at = timezone.now()
            submission.teacher_remarks = form.cleaned_data.get("teacher_remarks", "")
            submission.save(update_fields=["graded_at", "teacher_remarks"])
            messages.success(request, "Submission graded successfully.")
            return redirect(
                reverse("assessments:submissions_monitor", args=[assessment.pk])
            )
    else:
        form = GradeScoreForm(responses=subjective_responses)
    per_resp = {}
    present = {r.pk for r in subjective_responses}
    for resp, item_key, fname, point_max in getattr(form, "item_fields", []):
        if item_key is None:
            continue
        if resp.pk not in present:
            continue
        per_resp.setdefault(resp.pk, []).append(
            (item_key, form[fname], point_max)
        )
    subjective_grouped = [
        (resp, per_resp.get(resp.pk, [])) for resp in subjective_responses
    ]

    response_rows = []
    for resp in responses:
        for item_key, item in resp.part.items.items():
            is_correct = resp.item_scores.get(item_key, {}).get("is_correct")
            score = resp.item_scores.get(item_key, {}).get("score")
            answer = resp.response_data.get(item_key)
            if resp.part.question_type == AssessmentPart.TYPE_MATCHING:
                answer_display = " — ".join(
                    f"{k} -> {v}" for k, v in (answer or {}).items()
                )
            else:
                answer_display = answer if answer not in (None, "") else "—"
            response_rows.append(
                {
                    "part_order": resp.part.order,
                    "question": item.get("question", ""),
                    "answer": answer_display,
                    "type": resp.part.get_question_type_display(),
                    "max": item.get("point", 0),
                    "is_objective": resp.part.is_objective,
                    "is_correct": is_correct,
                    "score": score,
                }
            )
    context = {
        "assessment": assessment,
        "submission": submission,
        "subjective_grouped": subjective_grouped,
        "response_rows": response_rows,
        "form": form,
    }
    return render(request, "assessments/grade_submission.html", context)


@perm_required("access_assessment_repository")
def assessment_repository(request):
    if not request.user.groups.filter(name="teacher").exists():
        return HttpResponseForbidden("Only teachers can access the repository.")
    created = Assessment.objects.filter(
        created_by=request.user,
    ).select_related("subject", "academic_year").annotate(
        placement_count=Count("assignments", distinct=True)
    ).order_by("-created_at")
    context = {"assessments": created}
    return render(request, "assessments/assessment_repository.html", context)


# ---------------------------------------------------------------------------
# Grading formula + computed grade book
# ---------------------------------------------------------------------------
@perm_required("assessments.manually_grade_subjective_assessment")
def grading_formula(request, vc_pk):
    vc = get_object_or_404(
        VirtualClassroom.objects.select_related("academic_year", "subject"),
        pk=vc_pk,
    )
    formula = getattr(vc, "grading_formula", None)
    form = GradingComponentForm(
        request.POST or None,
        classroom=vc,
        formula=formula,
    )
    if request.method == "POST" and form.is_valid():
        if formula is None:
            from apps.academic_structure.models import AcademicStructureVersion

            structure_version = (
                getattr(vc.academic_year, "structure_version", None)
                or AcademicStructureVersion.objects.filter(
                    academic_year=vc.academic_year
                ).first()
            )
            if structure_version is None:
                kind = "TRIMESTER" if "TRIMESTER" in (vc.grading_period or "").upper() else (
                    "SEMESTER" if "SEMESTER" in (vc.grading_period or "").upper() else "QUARTER"
                )
                structure_version = AcademicStructureVersion.objects.create(
                    academic_year=vc.academic_year,
                    grading_period_type=kind,
                )
            formula = GradingFormula.objects.create(
                virtual_classroom=vc,
                structure_version=structure_version,
            )
        formula.components.all().delete()
        expected = 0
        for component_type, weight in form.cleaned_data.get("components", []):
            if weight:
                GradingComponent.objects.create(
                    formula=formula,
                    component_type=component_type,
                    weight_percentage=weight,
                )
                expected += weight
        messages.success(
            request,
            f"Grading formula saved ({formula.components.count()} component(s), weights total {expected}%).",
        )
        return redirect(reverse("assessments:grading_formula", args=[vc.pk]))
    context = {"vc": vc, "form": form, "formula": formula}
    return render(request, "assessments/grading_formula.html", context)


@perm_required("assessments.view_grade_book")
def grade_book(request, vc_pk):
    vc = get_object_or_404(
        VirtualClassroom.objects.select_related("academic_year", "subject", "section"),
        pk=vc_pk,
    )
    formula = getattr(vc, "grading_formula", None)
    enrollments = VirtualClassroomEnrollment.objects.filter(
        virtual_classroom=vc,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).select_related("student")
    grades = {}
    if request.method == "POST":
        if not has_permission(request.user, "assessments.compute_student_grade"):
            return HttpResponseForbidden(
                "You do not have permission to compute grades."
            )
        if not formula:
            messages.error(
                request, "No grading formula is configured for this class."
            )
            return redirect(reverse("assessments:grade_book", args=[vc.pk]))
        from .services import compute_grades_for_classroom

        result = compute_grades_for_classroom(vc, formula)
        messages.success(
            request,
            f"Computed {result['grades']} grade record(s) for {result['students']} student(s).",
        )
        return redirect(reverse("assessments:grade_book", args=[vc.pk]))
    if formula:
        grades = {
            g.student_id: g
            for g in StudentGrade.objects.filter(
                student_id__in=enrollments.values_list("student_id", flat=True),
                subject=vc.subject,
                academic_year=vc.academic_year,
                grading_period=vc.grading_period or "",
            )
        }
    context = {
        "vc": vc,
        "formula": formula,
        "rows": [(e, grades.get(e.student_id)) for e in enrollments],
    }
    return render(request, "assessments/grade_book.html", context)


def _classrooms_for_performance(user):
    qs = VirtualClassroom.objects.select_related(
        "section", "subject", "teacher"
    ).filter(status=VirtualClassroom.STATUS_ACTIVE)
    if user.is_superuser or user.groups.filter(name__in=ADMIN_ROLES).exists():
        return qs
    if user.groups.filter(name="teacher").exists():
        return qs.filter(teacher=user)
    if user.groups.filter(name="student").exists() and hasattr(user, "student_profile"):
        return qs.filter(
            enrollments__student=user.student_profile,
            enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).distinct()
    return qs.none()


@perm_required("assessments.view_student_performance_tracking")
def performance_tracking(request):
    """Consolidated per-student performance (grades + attendance) by classroom."""
    classroom_rows = []
    for vc in _classrooms_for_performance(request.user):
        enrollments = list(
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=vc,
                status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            ).select_related("student")
        )
        assessments = list(vc.assessment_assignments.select_related("assessment"))
        assessment_max = {}
        for aa in assessments:
            total = aa.assessment.parts.aggregate(total=Sum("points"))["total"] or 0
            assessment_max[aa.assessment_id] = float(total)

        student_rows = []
        for e in enrollments:
            subs = Submission.objects.filter(
                assessment_assignment__virtual_classroom=vc,
                student=e.student,
                total_score__isnull=False,
            )
            ssum = 0.0
            smax = 0.0
            for s in subs:
                mx = assessment_max.get(s.assessment_assignment.assessment_id, 0)
                if mx and s.total_score is not None:
                    ssum += float(s.total_score)
                    smax += mx
            avg_pct = round(ssum / smax * 100) if smax else 0

            att_rows = AttendanceRecord.objects.filter(
                session__virtual_classroom=vc,
                student=e.student,
            )
            present = att_rows.filter(
                status__in=[
                    AttendanceRecord.PRESENT_ON_TIME,
                    AttendanceRecord.PRESENT_LATE,
                ]
            ).count()
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

        classroom_rows.append(
            {
                "classroom": vc,
                "student_rows": student_rows,
                "student_count": len(enrollments),
                "assessment_count": len(assessments),
            }
        )

    return render(
        request,
        "assessments/performance_tracking.html",
        {"classroom_rows": classroom_rows},
    )