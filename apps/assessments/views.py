from decimal import Decimal

from datetime import timedelta
import logging
import json
import mimetypes
import os
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.campus import (
    WHOLE_SCHOOL,
    assessments_for_campus,
    campus_scopes_for,
    scoped_classrooms,
    user_can_read_grade,
)
from apps.accounts.decorators import has_permission, perm_required
from apps.attendance.models import AttendanceRecord
from apps.communication import notifications as notif_service
from apps.communication.models import Notification
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

def _notify(vc_ids, actor, kinds):
    """Tell each classroom's students their assessments/scores changed."""
    from apps.communication.realtime import notify_enrolled_students

    for vc_id in [i for i in (vc_ids or []) if i]:
        try:
            notify_enrolled_students(vc_id, kind=list(kinds), actor=actor, exclude=actor)
        except Exception:  # realtime must never block a save
            pass


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
    AssessmentAttempt,
    AssessmentPart,
    AssessmentProctoringEvent,
    GradingComponent,
    GradingFormula,
    StudentGrade,
    Submission,
    SubmissionResponse,
)
from .services import trigger_missed_notifications


ADMIN_ROLES = (
    "registrar",
    "assistant_principal",
    "system_admin",
    "principal",
    "elementary_principal",
    "quality_audit",
)

logger = logging.getLogger(__name__)


def _assessments_for_user(user):
    qs = Assessment.objects.select_related("subject", "created_by", "academic_year")
    if user.is_superuser or user.groups.filter(name__in=ADMIN_ROLES).exists():
        # Campus-bound principals only read their own grade bands; the rest of
        # ADMIN_ROLES (registrar, QA, system admin) reads the whole school.
        scopes = campus_scopes_for(user)
        if WHOLE_SCHOOL in scopes:
            return qs
        return assessments_for_campus(qs, scopes)
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


def _teacher_subject_ids(user):
    from apps.academic_structure.models import Subject

    return (
        Subject.objects.filter(
            virtual_classrooms__teacher_id=user.id,
            virtual_classrooms__status="active",
        )
        .values_list("id", flat=True)
        .distinct()
    )


def _can_view_assessment(user, assessment):
    """Object-level read check."""
    if user.is_superuser:
        return True
    if user.groups.filter(name__in=ADMIN_ROLES).exists():
        scopes = campus_scopes_for(user)
        if WHOLE_SCHOOL in scopes:
            return True
        return assessment.subject_id is not None and user_can_read_grade(
            user, assessment.subject.grade_level
        )
    if user.groups.filter(name="teacher").exists():
        return assessment.subject_id in set(_teacher_subject_ids(user))
    if hasattr(user, "student_profile"):
        return AssessmentAssignment.objects.filter(
            assessment=assessment,
            virtual_classroom__enrollments__student=user.student_profile,
            virtual_classroom__enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).exists()
    return False


def _can_edit_assessment(user, assessment):
    if user.is_superuser or user.groups.filter(name__in=ADMIN_ROLES).exists():
        scopes = campus_scopes_for(user)
        if WHOLE_SCHOOL in scopes:
            return True
        try:
            from apps.accounts.models import TeacherAssignment
            grades = set(
                TeacherAssignment.objects.filter(teacher=user, is_active=True).values_list(
                    "section__grade_level", flat=True
                )
            )
            return assessment.subject_id and assessment.subject.grade_level in grades
        except Exception:
            return assessment.subject_id and user_can_read_grade(user, assessment.subject.grade_level)
    if user.groups.filter(name="teacher").exists():
        return (
            assessment.created_by_id == user.id
            and assessment.subject_id in set(_teacher_subject_ids(user))
        )
    return False


def _student_assessment_dashboard_url(assessment_id, classroom_id=None, notice=None):
    """Build the current student SPA deep link for one assessment."""
    params = {
        "notification_tab": "assessments",
        "notification_record_type": "assessment",
        "notification_record": assessment_id,
    }
    if classroom_id:
        params["notification_classroom"] = classroom_id
    if notice:
        params["assessment_notice"] = notice
    return "{}?{}".format(reverse("dashboards:student"), urlencode(params))


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
        "assessments": assessments.order_by(
            "subject__grade_level", "subject__name", "title", "pk"
        ),
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
    form.fields["subject"].queryset = form.fields["subject"].queryset.filter(
        pk__in=_teacher_subject_ids(request.user)
    )
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
    if not _can_view_assessment(request.user, assessment):
        return HttpResponseForbidden("You do not have access to this assessment.")
    if request.user.groups.filter(name="student").exists() and hasattr(request.user, "student_profile"):
        assignment = AssessmentAssignment.objects.filter(
            assessment=assessment,
            virtual_classroom__enrollments__student=request.user.student_profile,
            virtual_classroom__enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).select_related("virtual_classroom").first()
        if not assignment:
            return HttpResponseForbidden("You do not have access to this assessment.")
        return redirect(_student_assessment_dashboard_url(
            assessment.pk, assignment.virtual_classroom_id
        ))
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


def _item_correct_answer(item):
    """Human-readable correct answer for one compiled item, for display."""
    if not isinstance(item, dict):
        return ""
    if "correct_matches" in item:  # matching_type
        matches = item.get("correct_matches") or {}
        if isinstance(matches, dict):
            return "; ".join("{} \u2192 {}".format(k, v) for k, v in matches.items())
        return str(matches)
    if "accepted_answers" in item:  # identification / short_answer
        answers = item.get("accepted_answers") or []
        if isinstance(answers, (list, tuple)):
            return ", ".join(str(a) for a in answers)
        return str(answers)
    if "correct_answer" in item:  # multiple choice, true_false, numeric
        correct = item.get("correct_answer")
        if isinstance(correct, (list, tuple)):
            return ", ".join(str(c) for c in correct)
        return str(correct)
    return ""


def _format_student_answer(answer):
    if answer is None or answer == "":
        return ""
    if isinstance(answer, dict):
        return "; ".join("{} \u2192 {}".format(k, v) for k, v in answer.items())
    if isinstance(answer, (list, tuple)):
        return ", ".join(str(a) for a in answer)
    return str(answer)


@perm_required("view_assessment_custom")
def assessment_result(request, pk):
    """Student-facing result sheet for one assessment.

    Shows the student's own answers alongside the correct answers, the
    per-item and per-part scores, and the overall total. This is the target
    of the "View" action on an assessment notification.
    """
    assessment = get_object_or_404(
        Assessment.objects.select_related("subject", "created_by"),
        pk=pk,
    )
    if not _can_view_assessment(request.user, assessment):
        return HttpResponseForbidden("You do not have access to this assessment.")
    profile = getattr(request.user, "student_profile", None)
    if profile is None:
        return HttpResponseForbidden("Only students can view their own results.")
    submission = (
        Submission.objects.filter(
            assessment_assignment__assessment=assessment,
            student=profile,
        )
        .select_related("assessment_assignment__virtual_classroom")
        .order_by("-submitted_at")
        .first()
    )
    if submission is None:
        messages.info(request, "You have not submitted this assessment yet.")
        return redirect(reverse("assessments:assessment_detail", args=[assessment.pk]))
    responses = {r.part_id: r for r in submission.responses.select_related("part").all()}
    part_rows = []
    for part in assessment.parts.order_by("order"):
        resp = responses.get(part.pk)
        item_meta = (resp.item_scores if resp else {}) or {}
        response_data = (resp.response_data if resp else {}) or {}
        rows = []
        for item_key, item in (part.items or {}).items():
            meta = item_meta.get(item_key) or {}
            score = meta.get("score")
            rows.append(
                {
                    "question": item.get("question", ""),
                    "answer": _format_student_answer(response_data.get(item_key)),
                    "correct": _item_correct_answer(item),
                    "point": item.get("point", 0),
                    "score": score,
                    "is_correct": meta.get("is_correct"),
                    "feedback": meta.get("feedback", ""),
                    "pending": score is None,
                }
            )
        part_rows.append(
            {
                "part": part,
                "rows": rows,
                "score": resp.score if resp else None,
            }
        )
    assignment = submission.assessment_assignment
    vc = assignment.virtual_classroom
    pts = int(assessment.total_possible_score or 0)
    score = float(submission.total_score or 0)
    pct = round(100.0 * score / pts) if pts else 0
    context = {
        "assessment": assessment,
        "submission": submission,
        "part_rows": part_rows,
        "classroom": vc,
        "pts": pts,
        "score": score,
        "pct": pct,
        "grading_status": submission.get_grading_status_display(),
        "teacher_remarks": submission.teacher_remarks or "",
    }
    return render(request, "assessments/assessment_result.html", context)


@perm_required("view_assessment_custom")
def assessment_attachment(request, pk, part_pk):
    """Serve an assessment attachment only to users who can read its record."""
    assessment = get_object_or_404(Assessment, pk=pk)
    if not _can_view_assessment(request.user, assessment):
        return HttpResponseForbidden("You do not have access to this assessment.")
    part = get_object_or_404(AssessmentPart, pk=part_pk, assessment=assessment)
    if not part.attached_file:
        return HttpResponse("This assessment attachment is unavailable.", status=404)
    try:
        name = os.path.basename(part.attached_file.name)
        content_type, _ = mimetypes.guess_type(name)
        return FileResponse(
            part.attached_file.open("rb"),
            as_attachment=True,
            filename=name,
            content_type=content_type or "application/octet-stream",
        )
    except FileNotFoundError:
        return HttpResponse("This assessment attachment is unavailable.", status=404)


@perm_required("update_assessment_custom")
def assessment_update(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    if not _can_edit_assessment(request.user, assessment):
        return HttpResponseForbidden("You cannot update this assessment.")
    form = AssessmentForm(request.POST or None, instance=assessment)
    if request.user.groups.filter(name="teacher").exists() and not request.user.is_superuser:
        form.fields["subject"].queryset = form.fields["subject"].queryset.filter(
            pk__in=_teacher_subject_ids(request.user)
        )
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
        if not _can_edit_assessment(request.user, assessment):
            return HttpResponseForbidden("You cannot archive this assessment.")
        assessment.status = Assessment.STATUS_ARCHIVED
        assessment.save(update_fields=["status"])
        messages.success(request, "Assessment archived.")
    return redirect(reverse("assessments:assessment_list"))


@perm_required("trigger_missed_assessment_notification")
def trigger_notifications(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    if not _can_view_assessment(request.user, assessment):
        return HttpResponseForbidden("You do not have access to this assessment.")
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
    if not _can_edit_assessment(request.user, assessment):
        return HttpResponseForbidden("You cannot assign this assessment.")
    if assessment.status == Assessment.STATUS_ARCHIVED:
        messages.error(request, "Cannot assign an archived assessment.")
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    form = AssessmentAssignForm(
        request.POST or None, assessment=assessment
    )
    if request.user.groups.filter(name="teacher").exists() and not request.user.is_superuser:
        form.fields["virtual_classrooms"].queryset = form.fields[
            "virtual_classrooms"
        ].queryset.filter(teacher=request.user)
    if request.method == "POST" and form.is_valid():
        if form.cleaned_data.get("assign_entire_subject"):
            selected_vcs = VirtualClassroom.objects.filter(
                status="active",
                subject=assessment.subject,
            ).exclude(
                assessment_assignments__assessment=assessment,
            )
            if request.user.groups.filter(name="teacher").exists() and not request.user.is_superuser:
                selected_vcs = selected_vcs.filter(teacher=request.user)
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
            assignment = AssessmentAssignment.objects.create(
                assessment=assessment,
                virtual_classroom=vc,
                assigned_by=request.user,
                is_original=is_original,
            )
            # Bell notification for the students in this classroom. Skipped
            # automatically when the assessment is still scheduled.
            try:
                notif_service.notify_assessment_assignment(assignment)
            except Exception:
                pass
            created_count += 1
        if created_count:
            messages.success(
                request, f"Assessment assigned to {created_count} classroom(s)."
            )
            _notify([vc.pk for vc in selected_vcs], request.user, ["assessments"])
        else:
            messages.info(request, "No new assignments were created.")
        return redirect(
            reverse("assessments:assessment_detail", args=[assessment.pk])
        )
    context = {"form": form, "assessment": assessment}
    return render(request, "assessments/assessment_assign.html", context)


PROCTORING_COOLDOWN_SECONDS = 5


@login_required
@require_POST
def record_proctoring_event(request, pk):
    """Record a client-detected focus/visibility/screenshot signal.

    The browser can only *observe* these signals; this stores them so a teacher
    can review what happened. Only an actively enrolled student assigned this
    assessment may write a row, and rapid duplicates from the burst of events a
    single tab switch fires collapse into one.
    """
    assessment = get_object_or_404(Assessment, pk=pk)
    student = getattr(request.user, "student_profile", None)
    if student is None:
        return JsonResponse({"ok": False, "error": "students_only"}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    assignments = AssessmentAssignment.objects.filter(
        assessment=assessment,
        virtual_classroom__enrollments__student=student,
        virtual_classroom__enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).select_related("virtual_classroom", "virtual_classroom__teacher").distinct()
    assignment_id = request.GET.get("assignment_id") or request.POST.get("assignment_id") or payload.get("assignment_id")
    if assignment_id:
        if not str(assignment_id).isdigit():
            return JsonResponse({"ok": False, "error": "not_enrolled"}, status=403)
        assignments = assignments.filter(pk=assignment_id)
    assignments = list(assignments.order_by("virtual_classroom_id", "pk"))
    if not assignments:
        return JsonResponse({"ok": False, "error": "not_enrolled"}, status=403)
    event_type = payload.get("event_type") or request.POST.get("event_type") or ""
    valid_types = {choice for choice, _ in AssessmentProctoringEvent.EVENT_TYPE_CHOICES}
    if event_type not in valid_types:
        return JsonResponse({"ok": False, "error": "unknown_event"}, status=400)

    metadata = {}
    for key in ("source", "hidden_seconds", "detail"):
        if key in payload:
            metadata[key] = payload[key]
    if isinstance(payload.get("metadata"), dict):
        metadata.update(payload["metadata"])

    since = timezone.now() - timedelta(seconds=PROCTORING_COOLDOWN_SECONDS)
    recent_events = AssessmentProctoringEvent.objects.filter(
        assessment=assessment, student=student, created_at__gte=since,
    )
    switch_events = {
        AssessmentProctoringEvent.EVENT_FOCUS_LOST,
        AssessmentProctoringEvent.EVENT_VISIBILITY_HIDDEN,
    }
    duplicate_types = switch_events if event_type in switch_events else {event_type}
    if recent_events.filter(event_type__in=duplicate_types).exists():
        return JsonResponse({"ok": True, "recorded": False, "reason": "duplicate"})

    event = AssessmentProctoringEvent.objects.create(
        assessment=assessment,
        student=student,
        event_type=event_type,
        metadata=metadata,
    )

    # Notify each assigned classroom teacher once. The persistent bell row is
    # idempotent per teacher/event and opens the teacher's activity log.
    if event_type not in {
        AssessmentProctoringEvent.EVENT_FOCUS_GAINED,
        AssessmentProctoringEvent.EVENT_VISIBILITY_VISIBLE,
    }:
        title = "Assessment integrity alert"
        event_label = event.get_event_type_display().lower()
        student_name = " ".join(filter(None, [student.first_name, student.last_name])) or student.lrn
        message = '{} {} during "{}".'.format(student_name, event_label, assessment.title)
        notified_teachers = set()
        for assignment in assignments:
            teacher = assignment.virtual_classroom.teacher
            if not teacher or teacher.pk in notified_teachers:
                continue
            notified_teachers.add(teacher.pk)
            try:
                Notification.objects.get_or_create(
                    recipient=teacher,
                    notification_type=Notification.TYPE_ASSESSMENT,
                    source_type="assessment_proctoring_event",
                    source_id=event.pk,
                    defaults={
                        "virtual_classroom": assignment.virtual_classroom,
                        "title": title,
                        "message": message,
                    },
                )
            except Exception:
                logger.exception("Could not notify teacher about assessment proctoring event %s", event.pk)
    return JsonResponse({"ok": True, "recorded": True})


@perm_required("complete_assessment_custom")
def assessment_submit(request, pk):
    json_mode = (
        request.GET.get("format") == "json"
        or request.headers.get("Accept", "").startswith("application/json")
        or request.content_type == "application/json"
    )

    def fail(message, status=400, code="assessment_unavailable"):
        if json_mode:
            return JsonResponse({"ok": False, "code": code, "message": message}, status=status)
        if hasattr(request.user, "student_profile"):
            classroom_id = None
            assignment_id = request.POST.get("assignment_id") or request.GET.get("assignment_id")
            if assignment_id:
                classroom_id = AssessmentAssignment.objects.filter(
                    pk=assignment_id,
                    assessment_id=pk,
                    virtual_classroom__enrollments__student=request.user.student_profile,
                    virtual_classroom__enrollments__status=VirtualClassroomEnrollment.STATUS_ACTIVE,
                ).values_list("virtual_classroom_id", flat=True).first()
            return redirect(_student_assessment_dashboard_url(pk, classroom_id, code))
        messages.error(request, message)
        return redirect(reverse("assessments:assessment_detail", args=[pk]))

    assessment = get_object_or_404(Assessment, pk=pk)
    if not hasattr(request.user, "student_profile"):
        if json_mode:
            return JsonResponse({"ok": False, "message": "Only students can submit assessments."}, status=403)
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
        return fail("You are not enrolled in any classroom for this assessment.", 403, "not_enrolled")
    assignment_id = request.POST.get("assignment_id") or request.GET.get("assignment_id")
    selected_assignment = assignments.filter(pk=assignment_id).first() if assignment_id else assignments.first()
    if selected_assignment is None:
        return fail("This classroom assessment is not available to your account.", 403, "assignment_unavailable")
    from .scheduling import assessment_window, attempt_expiry

    state = assessment_window(assessment)
    if state == "upcoming":
        return fail("This assessment is not yet available.", 403, "not_yet_available")
    if state == "closed":
        return fail("This assessment has closed and is no longer accepting submissions.", 403, "assessment_closed")
    existing = Submission.objects.filter(
        assessment_assignment=selected_assignment,
        student=student,
    ).first()
    if existing:
        return fail("You have already submitted this assessment.", 409, "already_submitted")
    now = timezone.now()
    attempt, _ = AssessmentAttempt.objects.get_or_create(
        assignment=selected_assignment,
        student=student,
        defaults={
            "started_at": now,
            "expires_at": attempt_expiry(assessment, now),
        },
    )
    if attempt.submitted_at:
        if json_mode:
            return JsonResponse({"ok": False, "code": "already_submitted", "message": "This assessment attempt has already been submitted."}, status=409)
        return redirect(_student_assessment_dashboard_url(
            assessment.pk, selected_assignment.virtual_classroom_id, "already_submitted"
        ))
    parts = list(assessment.parts.order_by("order"))
    if request.method == "POST":
        json_payload = {}
        if request.content_type == "application/json":
            try:
                json_payload = json.loads(request.body or b"{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                return JsonResponse({"ok": False, "message": "Invalid assessment submission data."}, status=400)
        submitted_attempt_id = json_payload.get("attempt_id") if json_payload else request.POST.get("attempt_id", "")
        if str(submitted_attempt_id or "") != str(attempt.pk):
            if json_mode:
                return JsonResponse({"ok": False, "message": "This assessment attempt is not valid for your account."}, status=403)
            return HttpResponseForbidden("This assessment attempt is not valid for your account.")
        now = timezone.now()
        if assessment.submission_deadline and now >= assessment.submission_deadline:
            if json_mode:
                return JsonResponse({"ok": False, "code": "assessment_closed", "message": "This assessment has closed. Your saved answers were kept."}, status=403)
            return redirect(_student_assessment_dashboard_url(
                assessment.pk, selected_assignment.virtual_classroom_id, "assessment_closed"
            ))
        post_data = json_payload.get("answers", {}) if request.content_type == "application/json" else request.POST
        if now >= attempt.expires_at:
            # A delayed submission cannot extend time or replace the last
            # successfully saved answers.
            post_data = attempt.answers
        form = SubmitForm(post_data, parts=parts)
        if json_mode and not form.is_valid():
            return JsonResponse({"ok": False, "message": "Please complete the required answers.", "errors": form.errors.get_json_data()}, status=400)
        if form.is_valid():
            with transaction.atomic():
                locked_attempt = AssessmentAttempt.objects.select_for_update().get(
                    pk=attempt.pk, assignment=selected_assignment, student=student
                )
                submission, created = Submission.objects.get_or_create(
                    assessment_assignment=selected_assignment, student=student
                )
                if created:
                    for part in parts:
                        response = SubmissionResponse(
                            submission=submission,
                            part=part,
                            response_data=form.response_data_for(part),
                        )
                        if part.is_objective:
                            response.compute_auto_score()
                        else:
                            response.save()
                    submission.recompute_total_score()
                locked_attempt.submitted_at = now
                locked_attempt.save(update_fields=["submitted_at"])
            messages.success(request, "Assessment submitted successfully.")
            if json_mode:
                return JsonResponse({"ok": True, "submitted": True, "message": "Assessment submitted successfully."})
            return redirect(_student_assessment_dashboard_url(
                assessment.pk, selected_assignment.virtual_classroom_id, "submitted"
            ))
    else:
        form = SubmitForm(initial=attempt.answers, parts=parts)
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
        "attempt": attempt,
        "remaining_seconds": max(0, int((attempt.expires_at - now).total_seconds())),
    }
    if json_mode:
        def field_schema(field_name):
            bound = form[field_name]
            raw = bound.field
            widget_name = raw.widget.__class__.__name__
            if widget_name == "CheckboxSelectMultiple":
                kind = "checkboxes"
            elif widget_name == "RadioSelect":
                kind = "radios"
            elif widget_name == "Select":
                kind = "select"
            elif raw.__class__.__name__ in ("DecimalField", "IntegerField", "FloatField"):
                kind = "number"
            elif widget_name == "Textarea":
                kind = "textarea"
            else:
                kind = "text"
            return {
                "name": field_name,
                "label": str(bound.label or ""),
                "kind": kind,
                "required": raw.required,
                "choices": [
                    {"value": str(value), "label": str(label)}
                    for value, label in raw.choices
                ] if hasattr(raw, "choices") else [],
                "value": attempt.answers.get(field_name, ""),
            }

        schema_parts = []
        for part, entries in form.groups:
            questions = []
            for item_key, names in entries:
                field_names = names if isinstance(names, list) else [names]
                item_data = part.items.get(item_key, {}) if isinstance(part.items, dict) else {}
                questions.append({
                    "key": str(item_key),
                    "fields": [field_schema(name) for name in field_names],
                    "image": item_data.get("image", "") if isinstance(item_data, dict) else "",
                    "choice_images": item_data.get("choice_images", []) if isinstance(item_data, dict) else [],
                })
            schema_parts.append({
                "title": part.get_question_type_display(),
                "question_text": part.question_text or "",
                "points": part.points,
                "questions": questions,
            })
        return JsonResponse({
            "ok": True,
            "assessment": {
                "id": assessment.pk,
                "title": assessment.title,
                "subject": assessment.subject.name if assessment.subject_id else selected_assignment.virtual_classroom.subject.name,
                "classroom": str(selected_assignment.virtual_classroom),
                "deadline": assessment.submission_deadline.isoformat() if assessment.submission_deadline else "",
            },
            "assignment_id": selected_assignment.pk,
            "attempt_id": attempt.pk,
            "revision": attempt.revision,
            "answers": attempt.answers,
            "remaining_seconds": context["remaining_seconds"],
            "parts": schema_parts,
        })
    if not json_mode:
        return redirect(_student_assessment_dashboard_url(
            assessment.pk, selected_assignment.virtual_classroom_id
        ))
    return render(request, "assessments/assessment_submit.html", context)


@perm_required("monitor_student_submissions")
def submissions_monitor(request, pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    if not _can_view_assessment(request.user, assessment):
        return HttpResponseForbidden("You do not have access to this assessment.")
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


@perm_required("auto_grade_objective_assessment")
def auto_grade_submission(request, pk, submission_pk):
    """Re-run auto-grading over every objective response of one submission.

    Submissions are auto-graded inline at submit time; this is the explicit
    entry point for a teacher to re-apply the answer key (for example after an
    item key was corrected), and it enforces the auto-grade permission that
    the submit path itself cannot.
    """
    assessment = get_object_or_404(Assessment, pk=pk)
    if not _can_view_assessment(request.user, assessment):
        return HttpResponseForbidden("You do not have access to this assessment.")
    submission = get_object_or_404(
        Submission,
        pk=submission_pk,
        assessment_assignment__assessment=assessment,
    )
    if request.method == "POST":
        graded = 0
        for response in submission.responses.select_related("part"):
            if response.part.is_objective:
                response.compute_auto_score()
                graded += 1
        submission.recompute_total_score()
        messages.success(
            request,
            f"Auto-graded {graded} objective response(s) for {submission.student}.",
        )
    return redirect(
        reverse("assessments:submissions_monitor", args=[assessment.pk])
    )


@perm_required("manually_grade_subjective_assessment")
def grade_submission(request, pk, submission_pk):
    assessment = get_object_or_404(Assessment, pk=pk)
    if not _can_view_assessment(request.user, assessment):
        return HttpResponseForbidden("You do not have access to this assessment.")
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
            vc_id = getattr(submission.assessment_assignment, "virtual_classroom_id", None)
            if vc_id:
                _notify([vc_id], request.user, ["scores", "assessments"])
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
    user = request.user
    user_groups = set(user.groups.values_list("name", flat=True))
    allowed_roles = {"teacher", "assistant_principal", "principal", "elementary_principal"}
    if not (user.is_superuser or (user_groups & allowed_roles)):
        return HttpResponseForbidden("You do not have access to the assessment repository.")
    from apps.academic_structure.models import Subject

    subject_ids = list(_teacher_subject_ids(user))
    if user.is_superuser or user_groups & {"assistant_principal", "principal", "elementary_principal"}:
        subject_ids = list(Subject.objects.values_list("id", flat=True))

    if user_groups & {"assistant_principal", "principal", "elementary_principal"} and not user.is_superuser and "teacher" not in user_groups:
        # Leadership sees all assessments in repository scope (whole school as per roles)
        qs = (
            Assessment.objects.all()
            .select_related("subject", "academic_year", "created_by")
            .annotate(placement_count=Count("assignments", distinct=True))
        )
    elif user_groups & {"assistant_principal", "principal", "elementary_principal"} and user.is_superuser is False and "teacher" in user_groups:
        qs = (
            Assessment.objects.filter(
                subject_id__in=subject_ids
            )
            .select_related("subject", "academic_year", "created_by")
            .annotate(placement_count=Count("assignments", distinct=True))
        )
    else:
        qs = (
            Assessment.objects.filter(
                subject_id__in=subject_ids
            )
            .select_related("subject", "academic_year", "created_by")
            .annotate(placement_count=Count("assignments", distinct=True))
        )
    subject_filter = request.GET.get("subject", "").strip()
    q = request.GET.get("q", "").strip()
    if subject_filter.isdigit():
        qs = qs.filter(subject_id=int(subject_filter))
    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(subject__name__icontains=q)
            | Q(assessment_method__icontains=q)
        )
    assessments = list(qs.order_by(
        "subject__grade_level", "subject__name", "title", "pk"
    ))
    for assessment in assessments:
        assessment.can_edit = _can_edit_assessment(user, assessment)
    if user.is_superuser or user_groups & {"assistant_principal", "principal", "elementary_principal"}:
        subjects = Subject.objects.filter(status=Subject.STATUS_ACTIVE).order_by("grade_level", "name")
    else:
        subjects = Subject.objects.filter(id__in=subject_ids).order_by("grade_level", "name")
    context = {
        "assessments": assessments,
        "subjects": subjects,
        "q": q,
        "subject_filter": subject_filter,
        "is_empty": not assessments,
    }
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
    if not user_can_read_grade(request.user, vc.section.grade_level):
        return HttpResponseForbidden("You cannot view this classroom's grade book.")
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


@perm_required("assessments.compute_student_grade")
def compute_grades(request, vc_pk):
    """Dedicated endpoint to (re)compute StudentGrade rows for a classroom.

    The grade book also computes on POST, but this endpoint exposes the
    capability on its own so it is reachable and permission-enforced
    independently of the grade book screen.
    """
    vc = get_object_or_404(
        VirtualClassroom.objects.select_related("academic_year", "subject", "section"),
        pk=vc_pk,
    )
    if not user_can_read_grade(request.user, vc.section.grade_level):
        return HttpResponseForbidden("You cannot compute grades for this classroom.")
    if request.method == "POST":
        formula = getattr(vc, "grading_formula", None)
        if not formula:
            messages.error(
                request, "No grading formula is configured for this class."
            )
        else:
            from .services import compute_grades_for_classroom

            result = compute_grades_for_classroom(vc, formula)
            messages.success(
                request,
                f"Computed {result['grades']} grade record(s) for {result['students']} student(s).",
            )
    return redirect(reverse("assessments:grade_book", args=[vc.pk]))


def _classrooms_for_performance(user):
    qs = VirtualClassroom.objects.select_related(
        "section", "subject", "teacher"
    ).filter(status=VirtualClassroom.STATUS_ACTIVE)
    if user.is_superuser or user.groups.filter(name__in=ADMIN_ROLES).exists():
        return scoped_classrooms(qs, user)
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
