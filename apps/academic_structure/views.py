from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.decorators import has_permission, perm_required
from apps.accounts.models import StudentProfile
from apps.accounts.ordering import order_students

from .forms import (
    AcademicYearForm,
    PromoteForm,
    RolloverForm,
    SectionForm,
    SectionStudentAssignForm,
    SectionStudentMoveForm,
    StructureFinalizeForm,
    SubjectForm,
    assignable_students,
)
from .models import (
    AcademicStructureVersion,
    AcademicYear,
    Section,
    StudentEnrollment,
    Subject,
)
from apps.virtual_classrooms.models import VirtualClassroom, VirtualClassroomEnrollment

from .services import rollover_year


@perm_required("academic_structure.view_academic_year")
def academic_year_list(request):
    years = AcademicYear.objects.all()
    active = years.filter(status=AcademicYear.STATUS_ACTIVE).first()
    context = {
        "years": years,
        "active_year": active,
        "total_years": years.count(),
        "total_sections": Section.objects.filter(status=Section.STATUS_ACTIVE).count(),
        "total_subjects": Subject.objects.filter(status=Subject.STATUS_ACTIVE).count(),
        "total_enrollments": VirtualClassroomEnrollment.objects.filter(
            status=VirtualClassroomEnrollment.STATUS_ACTIVE
        ).values("student_id").distinct().count(),
    }
    return render(request, "academic_structure/academic_year_list.html", context)


@perm_required("add_academic_year_custom")
def academic_year_add(request):
    form = AcademicYearForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if (
            form.cleaned_data.get("status") == AcademicYear.STATUS_ACTIVE
            and AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).exists()
        ):
            messages.error(request, "There is already an active Academic Year. Archive it first before activating another.")
            return render(
                request,
                "academic_structure/academic_year_form.html",
                {"form": form, "title": "Add Academic Year"},
            )
        try:
            year = form.save()
        except (IntegrityError, ValidationError):
            messages.error(request, "There is already an active Academic Year. Archive it first before activating another.")
            return render(
                request,
                "academic_structure/academic_year_form.html",
                {"form": form, "title": "Add Academic Year"},
            )
        messages.success(request, "Academic year created successfully.")
        return redirect(reverse("academic:academic_year_list"))
    return render(
        request,
        "academic_structure/academic_year_form.html",
        {"form": form, "title": "Add Academic Year"},
    )


@perm_required("update_academic_year")
def academic_year_update(request, pk):
    year = get_object_or_404(AcademicYear, pk=pk)
    form = AcademicYearForm(request.POST or None, instance=year)
    if request.method == "POST" and form.is_valid():
        if (
            form.cleaned_data.get("status") == AcademicYear.STATUS_ACTIVE
            and year.status != AcademicYear.STATUS_ACTIVE
            and AcademicYear.objects.filter(status=AcademicYear.STATUS_ACTIVE).exists()
        ):
            messages.error(request, "There is already an active Academic Year. Archive it first before activating another.")
            return render(
                request,
                "academic_structure/academic_year_form.html",
                {"form": form, "year": year, "title": "Update Academic Year"},
            )
        try:
            form.save()
        except (IntegrityError, ValidationError):
            messages.error(request, "There is already an active Academic Year. Archive it first before activating another.")
            return render(
                request,
                "academic_structure/academic_year_form.html",
                {"form": form, "year": year, "title": "Update Academic Year"},
            )
        messages.success(request, "Academic year updated successfully.")
        return redirect(reverse("academic:academic_year_list"))
    return render(
        request,
        "academic_structure/academic_year_form.html",
        {"form": form, "year": year, "title": "Update Academic Year"},
    )


@require_POST
@perm_required("archive_academic_year")
def academic_year_archive(request, pk):
    year = get_object_or_404(AcademicYear, pk=pk)
    now = timezone.now()
    if year.status == AcademicYear.STATUS_ACTIVE:
        Section.objects.filter(academic_year=year).update(status=Section.STATUS_ARCHIVED)
        Subject.objects.filter(academic_year=year).update(status=Subject.STATUS_ARCHIVED)
        VirtualClassroom.objects.filter(
            academic_year=year, status=VirtualClassroom.STATUS_ACTIVE
        ).update(status=VirtualClassroom.STATUS_ARCHIVED)
        VirtualClassroomEnrollment.objects.filter(
            virtual_classroom__academic_year=year,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).update(status=VirtualClassroomEnrollment.STATUS_REMOVED, removed_at=now)
        StudentEnrollment.objects.filter(
            academic_year=year, status=StudentEnrollment.STATUS_ENROLLED
        ).update(status=StudentEnrollment.STATUS_ARCHIVED, archived_at=now)
    year.status = AcademicYear.STATUS_ARCHIVED
    year.save()
    messages.success(request, "Academic year and its sections, subjects, and enrollments archived.")
    return redirect(reverse("academic:academic_year_list"))


@perm_required("manage_academic_structure_template")
def structure_finalize(request, pk):
    year = get_object_or_404(AcademicYear, pk=pk)
    version, created = AcademicStructureVersion.objects.get_or_create(
        academic_year=year,
        defaults={"grading_period_type": AcademicStructureVersion.QUARTER},
    )
    form = StructureFinalizeForm(request.POST or None, instance=version)
    if request.method == "POST" and form.is_valid():
        if version.is_finalized:
            messages.warning(request, f"Structure for {year.label} is already finalized. It cannot be finalized again.")
            return redirect(reverse("academic:academic_year_list"))
        active_sections = Section.objects.filter(
            academic_year=year,
            status=Section.STATUS_ACTIVE,
        )
        missing = []
        for section in active_sections:
            has_subject = Subject.objects.filter(
                academic_year=year,
                grade_level=section.grade_level,
                status=Subject.STATUS_ACTIVE,
            ).exists()
            if not has_subject:
                missing.append(section)
        if missing:
            messages.error(
                request,
                "Structure cannot be finalized. The following grade levels have active sections but no subjects: "
                + ", ".join(sorted({f"Grade {s.grade_level}" for s in missing})),
            )
            return render(
                request,
                "academic_structure/structure_finalize.html",
                {"form": form, "year": year, "version": version},
            )
        version = form.save(commit=False)
        version.is_finalized = True
        version.finalized_at = timezone.now()
        version.save()
        messages.success(request, f"Structure for {year.label} finalized.")
        return redirect(reverse("academic:academic_year_list"))
    return render(
        request,
        "academic_structure/structure_finalize.html",
        {"form": form, "year": year, "version": version},
    )


@perm_required("rollover_academic_year")
def year_rollover(request, pk):
    source_year = get_object_or_404(AcademicYear, pk=pk)
    form = RolloverForm(request.POST or None, source_year=source_year)
    if request.method == "POST" and form.is_valid():
        target_year = form.cleaned_data["target_academic_year"]
        try:
            report = rollover_year(source_year, target_year)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(
                request,
                "academic_structure/year_rollover.html",
                {"form": form, "source_year": source_year},
            )
        messages.success(
            request,
            f"Rolled over from {source_year.label} to {target_year.label}. {report.summary()}",
        )
        for entry in report.skipped:
            messages.warning(request, entry)
        return redirect(reverse("academic:academic_year_list"))
    return render(
        request,
        "academic_structure/year_rollover.html",
        {"form": form, "source_year": source_year},
    )


@perm_required("academic_structure.view_section_custom")
def section_list(request):
    sections = Section.objects.select_related("academic_year", "adviser").all()
    counts = dict(
        StudentEnrollment.objects.filter(status=StudentEnrollment.STATUS_ENROLLED)
        .values_list("section_id")
        .annotate(count=Count("student_id", distinct=True))
    )
    context = {
        "sections": sections,
        "enrolled_counts": counts,
        "total_sections": sections.filter(status=Section.STATUS_ACTIVE).count(),
        "archived_sections": sections.filter(status=Section.STATUS_ARCHIVED).count(),
    }
    return render(request, "academic_structure/section_list.html", context)


@perm_required("add_section_custom")
def section_add(request):
    form = SectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            section = form.save()
        except IntegrityError:
            messages.error(request, "A section with this name already exists for the selected grade and academic year.")
            return render(
                request,
                "academic_structure/section_form.html",
                {"form": form, "title": "Add Section"},
            )
        messages.success(request, "Section created successfully.")
        # A saved section is empty, so send the registrar straight to the roster
        # page where the students can be added. "next" skips it.
        if request.POST.get("save_and_add_students") and has_permission(
            request.user, "create_student_enrollment_inline"
        ):
            return redirect(reverse("academic:section_students", args=[section.pk]))
        return redirect(reverse("academic:section_list"))
    return render(
        request,
        "academic_structure/section_form.html",
        {"form": form, "title": "Add Section"},
    )


@perm_required("update_section_custom")
def section_update(request, pk):
    section = get_object_or_404(Section, pk=pk)
    form = SectionForm(request.POST or None, instance=section)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except IntegrityError:
            messages.error(request, "A section with this name already exists for the selected grade and academic year.")
            return render(
                request,
                "academic_structure/section_form.html",
                {"form": form, "section": section, "title": "Update Section"},
            )
        messages.success(request, "Section updated successfully.")
        return redirect(reverse("academic:section_list"))
    return render(
        request,
        "academic_structure/section_form.html",
        {"form": form, "section": section, "title": "Update Section"},
    )


def _enrol_students(section, students):
    """Attach ``students`` to ``section``, moving any that sit elsewhere.

    A learner holds only one active enrollment per academic year, so a student
    who is already placed *this* year has that row archived and a fresh one
    created here. A student arriving from an earlier year has no active row for
    this year, so this is simply their first enrollment -- their previous year's
    record stays on file either way. Returns the number actually enrolled.
    """
    enrolled = 0
    for student in students:
        StudentEnrollment.objects.filter(
            student=student,
            academic_year=section.academic_year,
            status=StudentEnrollment.STATUS_ENROLLED,
        ).update(status=StudentEnrollment.STATUS_ARCHIVED, archived_at=timezone.now())
        try:
            StudentEnrollment.objects.create(
                student=student,
                academic_year=section.academic_year,
                section=section,
                status=StudentEnrollment.STATUS_ENROLLED,
            )
            enrolled += 1
        except IntegrityError:
            # Another request enrolled them first; leave that row alone.
            continue
    return enrolled


@perm_required("create_student_enrollment_inline")
def section_students(request, pk):
    """The roster of one section: add learners, bulk-copy, or remove one.

    Creating a Section leaves it empty, so this is where a registrar puts
    learners in it -- one checkbox at a time, or in bulk by copying the roster
    of another section (last year's section of the same name, or a sister
    section this year).
    """
    section = get_object_or_404(
        Section.objects.select_related("academic_year", "adviser"), pk=pk
    )

    if request.method == "POST":
        if "copy_from_section" in request.POST:
            move_form = SectionStudentMoveForm(request.POST, section=section)
            if move_form.is_valid():
                source = move_form.cleaned_data["source_section"]
                # The roster is read from the source's *own* year, so copying
                # last year's section promotes that cohort into this year rather
                # than pulling in whoever happens to sit in a same-named section
                # now.
                source_students = order_students(
                    StudentProfile.objects.filter(
                        enrollments__academic_year=source.academic_year,
                        enrollments__section=source,
                        enrollments__status=StudentEnrollment.STATUS_ENROLLED,
                    ).distinct()
                )
                with transaction.atomic():
                    copied = _enrol_students(section, source_students)
                if copied:
                    messages.success(
                        request,
                        "Copied {} student(s) from {} into {}.".format(
                            copied, source, section
                        ),
                    )
                else:
                    messages.warning(
                        request,
                        "{} has no enrolled students to copy.".format(source),
                    )
                return redirect(reverse("academic:section_students", args=[section.pk]))
        else:
            assign_form = SectionStudentAssignForm(request.POST, section=section)
            if assign_form.is_valid():
                with transaction.atomic():
                    added = _enrol_students(section, assign_form.cleaned_data["students"])
                if added:
                    messages.success(
                        request, "Enrolled {} student(s) into {}.".format(added, section)
                    )
                else:
                    messages.info(request, "No students were enrolled.")
                return redirect(reverse("academic:section_students", args=[section.pk]))
            return render(
                request,
                "academic_structure/section_students.html",
                _section_students_context(section, assign_form=assign_form),
            )

    context = _section_students_context(
        section,
        assign_form=SectionStudentAssignForm(section=section),
        move_form=SectionStudentMoveForm(section=section),
    )
    return render(request, "academic_structure/section_students.html", context)


def _section_students_context(section, assign_form=None, move_form=None):
    enrolled = order_students(
        StudentEnrollment.objects.filter(
            section=section, status=StudentEnrollment.STATUS_ENROLLED
        ).select_related("student__user"),
        prefix="student__",
    )
    candidates = assignable_students(section)
    return {
        "section": section,
        "enrollments": enrolled,
        "enrolled_count": enrolled.count(),
        "assign_form": assign_form,
        "move_form": move_form,
        "candidates": candidates,
        "candidate_count": candidates.count(),
    }


@require_POST
@perm_required("archive_student_enrollment_inline")
def section_student_remove(request, pk, enrollment_pk):
    """Take one learner off the roster, leaving the learner record intact."""
    section = get_object_or_404(Section, pk=pk)
    enrollment = get_object_or_404(
        StudentEnrollment.objects.select_related("student"),
        pk=enrollment_pk,
        section=section,
    )
    if enrollment.status == StudentEnrollment.STATUS_ENROLLED:
        enrollment.status = StudentEnrollment.STATUS_ARCHIVED
        enrollment.archived_at = timezone.now()
        enrollment.save(update_fields=["status", "archived_at"])
        messages.success(
            request, "{} was removed from {}.".format(enrollment.student, section)
        )
    return redirect(reverse("academic:section_students", args=[section.pk]))


@require_POST
@perm_required("archive_section_custom")
def section_archive(request, pk):
    section = get_object_or_404(Section, pk=pk)
    now = timezone.now()
    section.status = Section.STATUS_ARCHIVED
    section.save()
    VirtualClassroom.objects.filter(
        section=section, status=VirtualClassroom.STATUS_ACTIVE
    ).update(status=VirtualClassroom.STATUS_ARCHIVED)
    VirtualClassroomEnrollment.objects.filter(
        virtual_classroom__section=section,
        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
    ).update(status=VirtualClassroomEnrollment.STATUS_REMOVED, removed_at=now)
    StudentEnrollment.objects.filter(
        section=section, status=StudentEnrollment.STATUS_ENROLLED
    ).update(status=StudentEnrollment.STATUS_ARCHIVED, archived_at=now)
    messages.success(request, "Section and its related enrollments archived.")
    return redirect(reverse("academic:section_list"))


@perm_required("academic_structure.view_subject_custom")
def subject_list(request):
    subjects = Subject.objects.select_related("academic_year").all()
    context = {
        "subjects": subjects,
        "total_subjects": subjects.filter(status=Subject.STATUS_ACTIVE).count(),
        "archived_subjects": subjects.filter(status=Subject.STATUS_ARCHIVED).count(),
    }
    return render(request, "academic_structure/subject_list.html", context)


@perm_required("add_subject_custom")
def subject_add(request):
    form = SubjectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except IntegrityError:
            messages.error(request, "A subject with this name already exists for the selected grade, academic year and grading period.")
            return render(
                request,
                "academic_structure/subject_form.html",
                {"form": form, "title": "Add Subject"},
            )
        messages.success(request, "Subject created successfully.")
        return redirect(reverse("academic:subject_list"))
    return render(
        request,
        "academic_structure/subject_form.html",
        {"form": form, "title": "Add Subject"},
    )


@perm_required("update_subject_custom")
def subject_update(request, pk):
    subject = get_object_or_404(Subject, pk=pk)
    form = SubjectForm(request.POST or None, instance=subject)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except IntegrityError:
            messages.error(request, "A subject with this name already exists for the selected grade, academic year and grading period.")
            return render(
                request,
                "academic_structure/subject_form.html",
                {"form": form, "subject": subject, "title": "Update Subject"},
            )
        messages.success(request, "Subject updated successfully.")
        return redirect(reverse("academic:subject_list"))
    return render(
        request,
        "academic_structure/subject_form.html",
        {"form": form, "subject": subject, "title": "Update Subject"},
    )


@require_POST
@perm_required("archive_subject_custom")
def subject_archive(request, pk):
    subject = get_object_or_404(Subject, pk=pk)
    subject.status = Subject.STATUS_ARCHIVED
    subject.save()
    messages.success(request, "Subject archived.")
    return redirect(reverse("academic:subject_list"))


@perm_required("manage_academic_structure_template")
def enrollment_list(request):
    enrollments = VirtualClassroomEnrollment.objects.select_related(
        "student",
        "virtual_classroom__academic_year",
        "virtual_classroom__section",
        "virtual_classroom__subject",
    ).all()
    years = AcademicYear.objects.all()
    sections = Section.objects.all()

    year_id = request.GET.get("year")
    section_id = request.GET.get("section")
    year_filter = None
    section_filter = None
    if year_id:
        year_filter = get_object_or_404(AcademicYear, pk=year_id)
        enrollments = enrollments.filter(virtual_classroom__academic_year=year_filter)
    if section_id:
        section_filter = get_object_or_404(Section, pk=section_id)
        enrollments = enrollments.filter(virtual_classroom__section=section_filter)

    context = {
        "enrollments": enrollments,
        "years": years,
        "sections": sections,
        "year_filter": year_filter,
        "section_filter": section_filter,
        "total_enrolled": enrollments.filter(status=VirtualClassroomEnrollment.STATUS_ACTIVE).values("student_id").distinct().count(),
    }
    return render(request, "academic_structure/enrollment_list.html", context)


@perm_required("manage_academic_structure_template")
def enrollment_add(request):
    messages.info(request, "Student enrollments are managed per Virtual Classroom.")
    return redirect(reverse("enrollment:dashboard"))


@require_POST
@perm_required("manage_academic_structure_template")
def enrollment_archive(request, pk):
    enrollment = get_object_or_404(VirtualClassroomEnrollment, pk=pk)
    enrollment.status = VirtualClassroomEnrollment.STATUS_REMOVED
    enrollment.removed_at = timezone.now()
    enrollment.save()
    messages.success(request, "Enrollment removed from the classroom.")
    return redirect(reverse("academic:enrollment_list"))


@perm_required("promote_student")
def promote_students(request):
    sections = Section.objects.select_related("academic_year").filter(
        status=Section.STATUS_ACTIVE
    )
    enrolled_counts = {
        row["virtual_classroom__section_id"]: row["count"]
        for row in VirtualClassroomEnrollment.objects.filter(
            status=VirtualClassroomEnrollment.STATUS_ACTIVE
        )
        .values("virtual_classroom__section_id")
        .annotate(count=Count("student_id", distinct=True))
    }
    section_rows = [
        {
            "name": s.name,
            "grade_level": s.grade_level,
            "academic_year_label": s.academic_year.label,
            "enrolled": enrolled_counts.get(s.pk, 0),
        }
        for s in sections
    ]
    promote_form = PromoteForm(request.POST or None)

    if request.method == "POST" and promote_form.is_valid():
        source = promote_form.cleaned_data["source_section"]
        target = promote_form.cleaned_data["target_section"]
        if target.academic_year_id != source.academic_year_id:
            messages.error(request, "Target section must belong to the same academic year as the source section.")
            return render(
                request,
                "academic_structure/promote.html",
                {"sections": section_rows, "promote_form": promote_form},
            )
        moved = 0
        moved_student_ids = set()
        enrollments = VirtualClassroomEnrollment.objects.filter(
            virtual_classroom__section=source,
            status=VirtualClassroomEnrollment.STATUS_ACTIVE,
        ).select_related("virtual_classroom")
        for enrollment in enrollments:
            target_vc = (
                VirtualClassroom.objects.filter(
                    academic_year=source.academic_year,
                    section=target,
                    subject=enrollment.virtual_classroom.subject,
                    status=VirtualClassroom.STATUS_ACTIVE,
                )
                .exclude(status=VirtualClassroom.STATUS_ARCHIVED)
                .first()
            )
            if not target_vc:
                continue
            try:
                with transaction.atomic():
                    VirtualClassroomEnrollment.objects.create(
                        virtual_classroom=target_vc,
                        student=enrollment.student,
                        status=VirtualClassroomEnrollment.STATUS_ACTIVE,
                    )
                    enrollment.status = VirtualClassroomEnrollment.STATUS_REMOVED
                    enrollment.removed_at = timezone.now()
                    enrollment.save()
                    moved += 1
                    moved_student_ids.add(enrollment.student_id)
            except IntegrityError:
                continue
        messages.success(request, f"Promoted {len(moved_student_ids)} student(s) to {target}.")
        return redirect(reverse("academic:enrollment_list"))

    return render(
        request,
        "academic_structure/promote.html",
        {"sections": section_rows, "promote_form": promote_form},
    )


@perm_required("validate_section_subject_coverage")
def validate_coverage(request):
    sections = Section.objects.select_related("academic_year").filter(
        status=Section.STATUS_ACTIVE
    )
    section_id = request.GET.get("section")
    section = None
    coverage = []
    subjects = Subject.objects.none()
    assigned_subject_ids = set()
    if section_id:
        section = get_object_or_404(Section, pk=section_id)
        subjects = Subject.objects.filter(
            academic_year=section.academic_year,
            grade_level=section.grade_level,
            status=Subject.STATUS_ACTIVE,
        )
        assigned_subject_ids = set(
            section.virtual_classrooms.filter(status="active").values_list(
                "subject_id", flat=True
            )
        )
    missing = 0
    for subject in subjects:
        has_assignment = subject.pk in assigned_subject_ids
        if not has_assignment:
            missing += 1
        coverage.append({"subject": subject, "has_assignment": has_assignment})
    context = {
        "sections": sections,
        "section": section,
        "coverage": coverage,
        "total_subjects": len(coverage),
        "missing_count": missing,
        "complete": len(coverage) > 0 and missing == 0,
    }
    return render(
        request,
        "academic_structure/validate_coverage.html",
        context,
    )
