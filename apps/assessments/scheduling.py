"""Shared schedule and clock rules for student assessment entry points."""

from datetime import timedelta

from django.utils import timezone

from .models import Assessment


def assessment_window(assessment, now=None):
    """Return ``available``, ``upcoming`` or ``closed`` using aware app time."""
    now = now or timezone.now()
    if assessment.status != Assessment.STATUS_ACTIVE:
        return "closed"
    if assessment.lifecycle_stage == Assessment.STAGE_COMPLETED:
        return "closed"
    if assessment.available_at and now < assessment.available_at:
        return "upcoming"
    if assessment.submission_deadline and now >= assessment.submission_deadline:
        return "closed"
    return "available"


def assessment_time_limit(assessment):
    """Return the time limit configured by the teacher for this assessment."""
    minutes = assessment.time_limit_minutes or 60
    return timedelta(minutes=max(1, minutes))


def attempt_expiry(assessment, started_at):
    """Cap the attempt clock at both its time limit and the class deadline."""
    expiry = started_at + assessment_time_limit(assessment)
    if assessment.submission_deadline and assessment.submission_deadline < expiry:
        expiry = assessment.submission_deadline
    return expiry
