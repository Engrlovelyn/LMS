"""
One student's view of one virtual classroom.

Used in two places so the first paint and every live refresh go through the
same code and cannot drift:

  * ``student_context`` (apps/dashboards/context.py) for the initial render
  * ``GET /api/my/classrooms/<id>/`` for updates pushed over the live socket

Everything here is scoped to a single ``VirtualClassroom`` that the student is
actively enrolled in, so a student can never receive another classroom's work.
"""

from django.utils import timezone

RESOURCE_ICONS = {
    "pdf": "fa-solid fa-file-pdf",
    "presentation": "fa-solid fa-file-powerpoint",
    "document": "fa-solid fa-file-word",
    "video": "fa-solid fa-video",
    "image": "fa-solid fa-file-image",
    "audio": "fa-solid fa-file-audio",
    "link": "fa-solid fa-link",
}

# Colours reused by the Materials / Assessments cards on the student dashboard.
ASSESSMENT_COLOURS = {
    "open": "#16a34a",
    "scheduled": "#1e5faf",
    "overdue": "#e11d48",
    "closed": "#64748b",
}


def _fmt_dt(value):
    return timezone.localtime(value).strftime("%b %d, Y · %I:%M %p") if value else None


def _fmt_d(value):
    return timezone.localtime(value).strftime("%b %d, Y") if value else None


def _subj_label(subject):
    if not subject:
        return ""
    name = subject.name
    grade = getattr(subject, "grade_level", None)
    return "{} {}".format(name, grade) if grade else name


def _ext(resource):
    if not resource or not resource.file:
        return ""
    name = resource.file.name
    return name.rsplit(".", 1)[-1].upper() if "." in name else ""


def materials_for(vc):
    """Learning materials placed in this classroom, newest first."""
    from apps.teaching_learning.models import LearningResourcePlacement

    rows = (
        LearningResourcePlacement.objects.filter(virtual_classroom=vc)
        .select_related("resource", "topic", "placed_by")
        .order_by("-placed_at")
    )
    out = []
    for pl in rows:
        res = pl.resource
        rtype = (res.resource_type if res else "document") or "document"
        has_file = bool(res and res.file)
        out.append(
            {
                "key": "mat-{}".format(pl.pk),
                "placement_id": pl.pk,
                "resource_id": res.pk if res else None,
                "name": (res.title[:80] if res else "Learning Resource"),
                "description": (res.description[:160] if res and res.description else ""),
                "icon": RESOURCE_ICONS.get(rtype, "fa-solid fa-file"),
                "color": "#1e5faf",
                "topic": (pl.topic.name if pl.topic_id and pl.topic else ""),
                "size": _ext(res) or ("Link" if rtype == "link" else "File"),
                "by": (pl.placed_by.get_full_name() if pl.placed_by_id else "Faculty"),
                "date": _fmt_d(pl.placed_at),
                "download_url": (
                    "/teaching/resources/{}/download/".format(res.pk) if has_file else ""
                ),
                "link_url": (res.external_link if rtype == "link" and res else ""),
            }
        )
    return out


def assessments_for(vc, profile, now=None):
    """Assessments assigned to this classroom, with this student's own state."""
    from apps.assessments.models import Assessment, AssessmentAssignment, Submission

    now = now or timezone.now()
    rows = (
        AssessmentAssignment.objects.filter(
            virtual_classroom=vc, assessment__status=Assessment.STATUS_ACTIVE
        )
        .select_related("assessment", "assessment__subject", "virtual_classroom")
        .order_by("assessment__submission_deadline")
    )
    submitted_ids = set(
        Submission.objects.filter(assessment_assignment__in=rows, student=profile)
        .values_list("assessment_assignment_id", flat=True)
    )
    out = []
    for aa in rows:
        a = aa.assessment
        subject = a.subject or vc.subject
        deadline = a.submission_deadline
        if aa.pk in submitted_ids:
            status = "closed"
        elif deadline and deadline < now:
            status = "overdue"
        elif a.lifecycle_stage == Assessment.STAGE_COMPLETED:
            status = "closed"
        elif deadline:
            status = "scheduled"
        else:
            status = "open"
        out.append(
            {
                "key": "assess-{}".format(aa.pk),
                "assignment_id": aa.pk,
                "classroom_id": vc.pk,
                "title": a.title,
                "subject": _subj_label(subject).strip(),
                "type": (a.get_assessment_method_display() or "assignment").lower(),
                "pts": int(a.total_possible_score or 0),
                "deadline": _fmt_dt(deadline) or "Open",
                "status": status,
                "color": ASSESSMENT_COLOURS.get(status, "#1e5faf"),
                "period": vc.grading_period or "Quarter 1",
                "instr": a.instructions or "",
                "submitted": aa.pk in submitted_ids,
            }
        )
    return out


def announcements_for(vc):
    """Class announcements posted in this classroom."""
    from apps.communication.models import ClassAnnouncement

    rows = (
        ClassAnnouncement.objects.filter(virtual_classroom=vc, status=ClassAnnouncement.STATUS_ACTIVE)
        .select_related("virtual_classroom__subject", "posted_by")
        .order_by("-posted_at")
    )
    return [
        {
            "key": "ann-{}".format(ca.pk),
            "classroom_id": vc.pk,
            "subject": _subj_label(ca.virtual_classroom.subject) if ca.virtual_classroom_id else "Class",
            "title": (ca.content[:60] if ca.content else "Class announcement"),
            "body": ca.content or "",
            "by": (ca.posted_by.get_full_name() if ca.posted_by_id else "Teacher"),
            "date": _fmt_d(ca.posted_at),
        }
        for ca in rows
    ]


def attendance_for(vc, profile):
    """This student's attendance record in this classroom."""
    from apps.attendance.models import AttendanceRecord, AttendanceSession

    records = (
        AttendanceRecord.objects.filter(session__virtual_classroom=vc, student=profile)
        .select_related("session")
        .order_by("-session__session_date")
    )
    buckets = {
        AttendanceRecord.PRESENT_ON_TIME: "present",
        AttendanceRecord.PRESENT_LATE: "late",
        AttendanceRecord.ABSENT_EXCUSED: "absent_excused",
        AttendanceRecord.ABSENT_UNEXCUSED: "absent_unexcused",
    }
    summary = {"present": 0, "late": 0, "absent_excused": 0, "absent_unexcused": 0}
    detail = []
    for rec in records:
        key = buckets.get(rec.status)
        if key:
            summary[key] += 1
        detail.append(
            {
                "date": rec.session.session_date.isoformat(),
                "status": rec.status,
                "label": rec.get_status_display(),
                "remarks": rec.remark or "",
            }
        )
    total = sum(summary.values())
    attended = summary["present"] + summary["late"]
    return {
        "summary": summary,
        "total": total,
        "attended": attended,
        "rate": round(100.0 * attended / total, 1) if total else None,
        "sessions": detail[:20],
    }


def student_classroom_payload(profile, vc, now=None):
    """Everything a student sees for one virtual classroom."""
    now = now or timezone.now()
    return {
        "classroom_id": vc.pk,
        "subject": _subj_label(vc.subject).strip(),
        "section": vc.section.name if vc.section_id else "",
        "teacher": vc.teacher.get_full_name() if vc.teacher_id else "",
        "teacher_id": vc.teacher_id,
        "materials": materials_for(vc),
        "assessments": assessments_for(vc, profile, now=now),
        "announcements": announcements_for(vc),
        "attendance": attendance_for(vc, profile),
        "generated_at": timezone.localtime(now).isoformat(),
    }
