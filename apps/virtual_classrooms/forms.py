from django import forms
from django.contrib.auth import get_user_model

from apps.accounts.models import StudentProfile
from apps.academic_structure.models import AcademicYear, Section, Subject

from .models import VirtualClassroom
from .services import assignable_teachers, teacher_conflict_in_section


def _teacher_meta(teacher):
    """Grade levels and subjects a teacher already handles, for the picker."""
    rooms = []
    try:
        rooms = list(teacher.teaching_classrooms.all())
    except Exception:
        rooms = []
    grades = sorted(
        {getattr(getattr(room.section, "grade_level", None), "__int__", lambda: room.section.grade_level)() if hasattr(room, "section") and room.section_id and hasattr(room.section, "grade_level") and isinstance(room.section.grade_level, int) is False else (room.section.grade_level if hasattr(room, "section") and room.section_id else None) for room in rooms}
    )
    grades = sorted({g for g in grades if g is not None})
    subjects = []
    try:
        subjects = sorted({room.subject.name for room in rooms if room.subject_id and room.subject})
    except Exception:
        subjects = []
    advised = []
    try:
        advised = sorted(
            {section.grade_level for section in teacher.advised_sections.all() if section and section.grade_level is not None}
        )
    except Exception:
        advised = []
    return grades, subjects, advised


class TeacherChoiceField(forms.ModelChoiceField):
    """Show a teacher's name plus the grades/subjects they already handle."""

    def label_from_instance(self, obj):
        name = obj.get_full_name() or obj.email
        grades, subjects, advised = _teacher_meta(obj)
        details = []
        if grades:
            details.append("Grades " + ", ".join(str(g) for g in grades))
        elif advised:
            details.append("Adviser of Grade " + ", ".join(str(g) for g in advised))
        if subjects:
            shown = subjects[:3]
            text = ", ".join(shown)
            if len(subjects) > len(shown):
                text += " +{} more".format(len(subjects) - len(shown))
            details.append(text)
        return "{} — {}".format(name, "; ".join(details)) if details else name


class TeacherSelect(forms.Select):
    """Select widget that tags each option with searchable metadata."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            grades, subjects, advised = _teacher_meta(instance)
            option["attrs"]["data-grades"] = ",".join(
                str(g) for g in sorted(set(grades) | set(advised))
            )
            option["attrs"]["data-name"] = (
                instance.get_full_name() or instance.email or ""
            ).lower()
            option["attrs"]["data-subjects"] = ",".join(subjects).lower()
        return option


class VirtualClassroomForm(forms.ModelForm):
    class Meta:
        model = VirtualClassroom
        fields = [
            "academic_year",
            "section",
            "subject",
            "teacher",
            "grading_period",
            "schedule",
            "theme_color",
            "background_image",
            "status",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["theme_color"].widget = forms.TextInput(
            attrs={"type": "color", "class": "form-control"}
        )
        self.fields["academic_year"].queryset = AcademicYear.objects.filter(status="ACTIVE")
        self.fields["section"].queryset = Section.objects.filter(status="ACTIVE")
        self.fields["subject"].queryset = Subject.objects.filter(status="ACTIVE")
        # The advisor picker must offer every eligible teacher in the school,
        # not just the ones already attached to this section. Grade levels and
        # subjects are shown as context only; they never remove a teacher from
        # the list, so a teacher who has not yet taught a grade can still be
        # assigned to it.
        from django.contrib.auth import get_user_model
        User = get_user_model()
        teacher_qs = (
            User.objects.filter(groups__name="teacher", is_active=True)
            .select_related("employee_profile")
            .prefetch_related(
                "teaching_classrooms__section",
                "teaching_classrooms__subject",
                "advised_sections",
            )
            .order_by("employee_profile__last_name", "employee_profile__first_name", "email")
        )
        self.fields["teacher"] = TeacherChoiceField(
            queryset=teacher_qs,
            required=False,
            label="Advisor / Teacher",
            empty_label="— Select a teacher —",
            widget=TeacherSelect(attrs={"class": "form-control"}),
            help_text="Every active teacher in the school is listed. Search or filter by grade to narrow the list.",
        )
        self.fields["teacher"].queryset = teacher_qs.distinct()

    def clean(self):
        cleaned = super().clean()
        academic_year = cleaned.get("academic_year")
        section = cleaned.get("section")
        subject = cleaned.get("subject")
        teacher = cleaned.get("teacher")
        if section and subject:
            if section.grade_level != subject.grade_level:
                self.add_error(
                    "subject",
                    "The subject's grade level must match the section's grade level.",
                )
            if academic_year and (
                section.academic_year_id != academic_year.pk
                or subject.academic_year_id != academic_year.pk
            ):
                self.add_error(
                    "academic_year",
                    "The section and subject must belong to the selected academic year.",
                )
        # One teacher per virtual classroom: a student must not see the same
        # teacher twice in their list, so no teacher may hold two classrooms in
        # the same section.
        if section and teacher:
            clash = teacher_conflict_in_section(
                section, teacher, exclude_classroom=self.instance
            )
            if clash is not None:
                self.add_error(
                    "teacher",
                    "{} already teaches {} in {}. Pick a different teacher so "
                    "each classroom has its own.".format(
                        teacher.get_full_name() or teacher.email,
                        clash.subject,
                        section,
                    ),
                )
        return cleaned


class VirtualClassroomEnrollForm(forms.Form):
    students = forms.ModelMultipleChoiceField(
        queryset=StudentProfile.objects.order_by("lrn"),
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["students"].label = False


class AddStudentForm(forms.Form):
    student = forms.ModelChoiceField(
        queryset=StudentProfile.objects.order_by("lrn"),
        empty_label="Select a student...",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["student"].widget.attrs["class"] = "form-control"
