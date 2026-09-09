from django import forms

from apps.academic_structure.models import Subject
from apps.virtual_classrooms.models import VirtualClassroom

from .models import Assessment, AssessmentPart


class AssessmentForm(forms.ModelForm):
    class Meta:
        model = Assessment
        fields = [
            "title",
            "subject",
            "assessment_type",
            "instructions",
            "submission_deadline",
            "accepted_file_format",
            "status",
        ]
        widgets = {
            "submission_deadline": forms.DateTimeInput(
                attrs={"type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["subject"].queryset = Subject.objects.filter(status="ACTIVE")
        if not self.instance.pk:
            self.fields["status"].initial = Assessment.STATUS_ACTIVE


class AssessmentPartForm(forms.Form):
    question_text = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
    )
    question_type = forms.ChoiceField(
        choices=[("", "Select type...")] + AssessmentPart.QUESTION_TYPE_CHOICES,
    )
    order = forms.IntegerField(min_value=1, initial=1)
    points = forms.DecimalField(max_digits=6, decimal_places=2, min_value=0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class AssessmentPartChoiceForm(forms.Form):
    choice_text = forms.CharField(max_length=255)
    is_correct = forms.BooleanField(required=False, initial=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class AssessmentAssignForm(forms.Form):
    virtual_classrooms = forms.ModelMultipleChoiceField(
        queryset=VirtualClassroom.objects.filter(status="active"),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Assign to Virtual Classrooms",
    )
    assign_entire_subject = forms.BooleanField(
        required=False,
        initial=False,
        label="Assign to the entire subject (all active classrooms)",
    )

    def __init__(self, *args, **kwargs):
        assessment = kwargs.pop("assessment", None)
        super().__init__(*args, **kwargs)
        if assessment:
            self.fields["virtual_classrooms"].queryset = (
                VirtualClassroom.objects.filter(
                    status="active",
                    subject=assessment.subject,
                ).exclude(
                    assessment_assignments__assessment=assessment,
                )
            )
        self.fields["virtual_classrooms"].label = False


class SubmitForm(forms.Form):
    def __init__(self, *args, **kwargs):
        parts = kwargs.pop("parts", [])
        super().__init__(*args, **kwargs)
        for part in parts:
            field_key = f"part_{part.pk}"
            if part.is_objective:
                choices = part.choices.all()
                choice_list = [(str(c.pk), c.choice_text) for c in choices]
                if part.question_type == AssessmentPart.TYPE_MULTIPLE_CHOICE:
                    self.fields[field_key] = forms.MultipleChoiceField(
                        choices=choice_list,
                        widget=forms.CheckboxSelectMultiple,
                        label=part.question_text,
                        required=True,
                    )
                else:
                    self.fields[field_key] = forms.ChoiceField(
                        choices=[("", "Select an answer...")] + choice_list,
                        widget=forms.RadioSelect,
                        label=part.question_text,
                        required=True,
                    )
            else:
                self.fields[field_key] = forms.CharField(
                    widget=forms.Textarea(attrs={"rows": 4, "class": "form-control"}),
                    label=part.question_text,
                    required=False,
                )
            self.fields[field_key].help_text = f"Points: {part.points}"


class GradeScoreForm(forms.Form):
    def __init__(self, *args, **kwargs):
        responses = kwargs.pop("responses", [])
        super().__init__(*args, **kwargs)
        for resp in responses:
            self.fields[f"score_{resp.pk}"] = forms.DecimalField(
                max_digits=6,
                decimal_places=2,
                min_value=0,
                label=f"{resp.part.question_text} - Score",
                initial=resp.score,
            )
        self.fields["teacher_remarks"] = forms.CharField(
            widget=forms.Textarea(attrs={"rows": 3}),
            label="Overall Remarks",
            required=False,
        )
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
