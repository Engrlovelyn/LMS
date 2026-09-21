from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.accounts.models import StudentProfile
from apps.virtual_classrooms.models import VirtualClassroomEnrollment

from .models import (
    Assessment,
    AssessmentAssignment,
    AssessmentNotification,
    StudentGrade,
    Submission,
)


def _enrolled_students(assessment):
    """Active students across all classrooms this assessment is assigned to."""
    student_ids = set()
    for assignment in assessment.assignments.select_related("virtual_classroom").all():
        student_ids.update(
            VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=assignment.virtual_classroom,
                status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            ).values_list("student_id", flat=True)
        )
    return StudentProfile.objects.filter(id__in=student_ids)


def _already_notified(assessment, event_type):
    return set(
        AssessmentNotification.objects.filter(
            assessment=assessment,
            event_type=event_type,
        ).values_list("student_id", flat=True)
    )


def trigger_availability_notifications(assessment=None):
    """
    Create 'available' notifications as soon as an assessment's
    availability timestamp passes (Rule j).
    """
    now = timezone.now()
    assessments = Assessment.objects.filter(
        status=Assessment.STATUS_ACTIVE,
        available_at__lte=now,
        available_at__isnull=False,
    )
    if assessment is not None:
        assessments = assessments.filter(pk=assessment.pk)

    summary = {"created": 0, "already": 0, "assessments": 0}
    for item in assessments:
        if not item.notification_settings.get("notify_on_availability", True):
            continue
        summary["assessments"] += 1
        existing = _already_notified(item, AssessmentNotification.EVENT_AVAILABLE)
        students = _enrolled_students(item)
        new_ids = [s.pk for s in students if s.pk not in existing]
        if new_ids:
            AssessmentNotification.objects.bulk_create(
                [
                    AssessmentNotification(
                        assessment=item,
                        student_id=sid,
                        event_type=AssessmentNotification.EVENT_AVAILABLE,
                        metadata={"available_at": item.available_at.isoformat()},
                    )
                    for sid in new_ids
                ]
            )
            summary["created"] += len(new_ids)
        summary["already"] += len(existing)
    return summary


def trigger_deadline_notifications(assessment=None):
    """
    Create 'deadline upcoming/nearing' notifications per the configured
    `deadline_reminder_hours_before` schedule (Rule j).
    """
    now = timezone.now()
    assessments = Assessment.objects.filter(status=Assessment.STATUS_ACTIVE)
    if assessment is not None:
        assessments = assessments.filter(pk=assessment.pk)

    summary = {"created": 0, "already": 0, "assessments": 0}
    for item in assessments:
        if item.submission_deadline <= now:
            continue
        schedule = item.notification_settings.get("deadline_reminder_hours_before", [])
        if not schedule:
            continue
        summary["assessments"] += 1
        for hours in schedule:
            hours = int(hours)
            window_start = item.submission_deadline - timedelta(hours=hours)
            if not (window_start <= now < window_start + timedelta(hours=1)):
                continue
            event_type = (
                AssessmentNotification.EVENT_DEADLINE_NEARING
                if hours < 24
                else AssessmentNotification.EVENT_DEADLINE_UPCOMING
            )
            existing = _already_notified(item, event_type)
            submitted = set(
                Submission.objects.filter(
                    assessment_assignment__assessment=item,
                ).values_list("student_id", flat=True)
            )
            new_ids = [
                s.pk
                for s in _enrolled_students(item)
                if s.pk not in existing and s.pk not in submitted
            ]
            if new_ids:
                AssessmentNotification.objects.bulk_create(
                    [
                        AssessmentNotification(
                            assessment=item,
                            student_id=sid,
                            event_type=event_type,
                            metadata={
                                "hours_before_deadline": hours,
                                "deadline": item.submission_deadline.isoformat(),
                            },
                        )
                        for sid in new_ids
                    ]
                )
                summary["created"] += len(new_ids)
            summary["already"] += len(existing)
    return summary


def trigger_missed_notifications(assessment=None):
    """
    Create 'missed' assessment notifications for enrolled students who have not
    submitted any response by the time the deadline has passed.

    Returns a dict summary: {"assessment": <Assessment>, "created": int, "already": int}.
    """
    now = timezone.now()
    assessments = Assessment.objects.filter(submission_deadline__lt=now)
    if assessment is not None:
        assessments = assessments.filter(pk=assessment.pk)

    summary = {"created": 0, "already": 0, "assessments": 0}
    for item in assessments:
        if not item.notification_settings.get("notify_on_missed", True):
            continue
        summary["assessments"] += 1
        existing = set(
            AssessmentNotification.objects.filter(
                assessment=item,
                event_type=AssessmentNotification.EVENT_MISSED,
            ).values_list("student_id", flat=True)
        )
        missing = set()
        for assignment in item.assignments.select_related("virtual_classroom").all():
            vc_students = VirtualClassroomEnrollment.objects.filter(
                virtual_classroom=assignment.virtual_classroom,
                status=VirtualClassroomEnrollment.STATUS_ACTIVE,
            ).values_list("student_id", flat=True)
            submissions = {
                s.student_id
                for s in Submission.objects.filter(
                    assessment_assignment=assignment,
                    student_id__in=vc_students,
                )
            }
            for student_id in vc_students:
                if (
                    student_id not in submissions
                    and student_id not in existing
                ):
                    missing.add(student_id)
        if missing:
            AssessmentNotification.objects.bulk_create(
                [
                    AssessmentNotification(
                        assessment=item,
                        student_id=student_id,
                        event_type=AssessmentNotification.EVENT_MISSED,
                        metadata={"deadline": item.submission_deadline.isoformat()},
                    )
                    for student_id in missing
                ]
            )
            summary["created"] += len(missing)
        summary["already"] += len(existing)
    return summary

def run_assessment_notifications(assessment=None):
    """Run all rule-(j) notification producers for assessments."""
    return {
        "availability": trigger_availability_notifications(assessment),
        "deadline": trigger_deadline_notifications(assessment),
        "missed": trigger_missed_notifications(assessment),
    }


@transaction.atomic
def compute_grades_for_classroom(vc, formula=None, commit=True):
    """
    Compute StudentGrade records for every active student in a virtual
    classroom using its GradingFormula.

    For each grading component type, the student's average percentage across
    all completed, assigned assessments of that type is weighted by the
    component's weight_percentage. The final grade is the weighted sum.

    Returns {"students": int, "grades": int}.
    """
    from .models import GradingComponent, GradingFormula, Submission

    formula = formula or getattr(vc, "grading_formula", None)
    if formula is None or not formula.components.exists():
        return {"students": 0, "grades": 0}

    enrollments = VirtualClassroomEnrollment.objects.filter(
        virtual_classroom=vc,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).select_related("student")
    students = [e.student for e in enrollments]

    components = {
        c.component_type: Decimal(str(c.weight_percentage))
        for c in formula.components.all()
    }
    if not components or sum(components.values()) != Decimal("100"):
        return {"students": 0, "grades": 0}

    assignment_ids = AssessmentAssignment.objects.filter(
        virtual_classroom=vc
    ).values_list("id", flat=True)
    submissions = Submission.objects.filter(
        assessment_assignment_id__in=assignment_ids,
        student_id__in=[s.pk for s in students],
        grading_status=Submission.GRADING_COMPLETE,
        total_score__isnull=False,
    ).select_related("assessment_assignment__assessment", "student")
    if not submissions:
        return {"students": 0, "grades": 0}

    # component_type -> [(student_id, pct)]
    per_component = {}
    for sub in submissions:
        assessment = sub.assessment_assignment.assessment
        possible = assessment.total_possible_score or 1
        pct = Decimal(str(sub.total_score)) * Decimal("100") / Decimal(str(possible))
        per_component.setdefault(assessment.assessment_type, []).append(
            (sub.student_id, pct)
        )

    grades_created = 0
    for student in students:
        total_weighted = Decimal("0")
        weighted_components = 0
        for component_type, weight in components.items():
            rows = per_component.get(component_type)
            if not rows:
                continue
            matched = [pct for sid, pct in rows if sid == student.pk]
            if not matched:
                continue
            avg = sum(matched, Decimal("0")) / len(matched)
            total_weighted += weight * avg
            weighted_components += 1
        if weighted_components == 0:
            continue
        grade_value = (total_weighted / Decimal("100")).quantize(Decimal("0.01"))
        if commit:
            StudentGrade.objects.update_or_create(
                student=student,
                subject=vc.subject,
                academic_year=vc.academic_year,
                grading_period=vc.grading_period,
                defaults={
                    "structure_version": formula.structure_version,
                    "grade_value": grade_value,
                },
            )
        grades_created += 1
    return {"students": len(students), "grades": grades_created}
