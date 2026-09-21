import json

from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from apps.academic_structure.models import Subject
from apps.virtual_classrooms.models import VirtualClassroom

from .models import Assessment, AssessmentPart, SubmissionResponse


class AssessmentForm(forms.ModelForm):
    class Meta:
        model = Assessment
        fields = [
            "title",
            "subject",
            "assessment_method",
            "assessment_type",
            "instructions",
            "available_at",
            "submission_deadline",
            "accepted_file_format",
            "status",
        ]
        widgets = {
            "available_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
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
        required=False,
        label="Part Instructions / Section Label",
    )
    question_type = forms.ChoiceField(
        choices=[("", "Select type...")] + AssessmentPart.QUESTION_TYPE_CHOICES,
        label="Question Type",
    )
    order = forms.IntegerField(
        min_value=1, initial=1, required=False, label="Order"
    )
    items_json = forms.CharField(
        widget=forms.Textarea(
            attrs={
                "rows": 12,
                "class": "form-control",
                "placeholder": '{\n  "item_1": {\n    "question": "...",\n    "point": 1\n  }\n}',
            }
        ),
        required=False,
        label="Items (JSON)",
        help_text="Compiled question bank as JSON keyed by item id. Required keys depend "
        "on the question type: multiple_choice/true_false -> question, choices, "
        "correct_answer, point; identification/short_answer -> question, accepted_answers, "
        "point; matching_type -> question, premises, targets, correct_matches, point; "
        "numeric -> question, correct_answer, point; essay-type -> question, point.",
    )

    def __init__(self, *args, **kwargs):
        self.part = kwargs.pop("part", None)
        super().__init__(*args, **kwargs)
        if self.part:
            self.fields["question_text"].initial = self.part.question_text
            self.fields["question_type"].initial = self.part.question_type
            self.fields["order"].initial = self.part.order
            self.fields["items_json"].initial = json.dumps(
                self.part.items, indent=2, ensure_ascii=False
            )
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned = super().clean()
        question_type = cleaned.get("question_type")
        items_json = cleaned.get("items_json") or ""
        if not question_type:
            raise ValidationError("Please select a question type.")
        try:
            items = json.loads(items_json) if items_json.strip() else {}
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Items JSON is invalid: {exc}")
        if not isinstance(items, dict) or not items:
            raise ValidationError("An assessment part must define at least one item.")
        required_keys = AssessmentPart._REQUIRED_ITEM_KEYS.get(
            question_type, ("question", "point")
        )
        for item_id, item in items.items():
            if not isinstance(item, dict):
                raise ValidationError(f"Item '{item_id}' must be a JSON object.")
            missing = [key for key in required_keys if key not in item]
            if missing:
                raise ValidationError(
                    f"Item '{item_id}' is missing required field(s): {', '.join(missing)}."
                )
            if "point" not in item:
                raise ValidationError(f"Item '{item_id}' must define a 'point' value.")
            if question_type == AssessmentPart.TYPE_MATCHING and "correct_matches" in item:
                unmatched = set(item.get("correct_matches", {})) - set(item.get("premises", []))
                if unmatched:
                    raise ValidationError(
                        f"Item '{item_id}' has correct_matches not listed in premises: "
                        f"{sorted(unmatched)}."
                    )
        cleaned["items"] = items
        return cleaned


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
    """
    Renders one form field per compiled item inside each AssessmentPart.

    Field naming scheme: `part_<part_pk>__<item_key>`. Matching-type items
    produce one ChoiceField per premise, named `part_<part_pk>__<item_key>_match_<idx>`.
    """

    def __init__(self, *args, **kwargs):
        parts = kwargs.pop("parts", [])
        super().__init__(*args, **kwargs)
        # (part, [(item_key, field_name_or_fields, field)]) for the template
        self.groups = []
        self.answers = {}  # part_pk -> {item_key: field_name}
        for part in parts:
            item_fields = []
            for item_key, item in part.items.items():
                base = f"part_{part.pk}__{item_key}"
                point = item.get("point", 0)
                question = item.get("question", item_key)
                if part.question_type == part.TYPE_MULTIPLE_CHOICE:
                    choices = item.get("choices", [])
                    correct = item.get("correct_answer")
                    if isinstance(correct, list):
                        self.fields[base] = forms.MultipleChoiceField(
                            choices=[(c, c) for c in choices],
                            widget=forms.CheckboxSelectMultiple,
                            label=question,
                            required=True,
                        )
                    else:
                        self.fields[base] = forms.ChoiceField(
                            choices=[("", "Select an answer...")] + [(c, c) for c in choices],
                            widget=forms.RadioSelect,
                            label=question,
                            required=True,
                        )
                elif part.question_type == part.TYPE_TRUE_FALSE:
                    self.fields[base] = forms.ChoiceField(
                        choices=[("", "Select an answer..."), ("True", "True"), ("False", "False")],
                        widget=forms.RadioSelect,
                        label=question,
                        required=True,
                    )
                elif part.question_type == part.TYPE_NUMERIC:
                    self.fields[base] = forms.DecimalField(
                        max_digits=10,
                        decimal_places=2,
                        label=question,
                        required=True,
                    )
                elif part.question_type == part.TYPE_MATCHING:
                    premises = item.get("premises", [])
                    targets = item.get("targets", [])
                    for idx, premise in enumerate(premises):
                        fname = f"{base}_match_{idx}"
                        self.fields[fname] = forms.ChoiceField(
                            choices=[("", "Select match...")] + [(t, t) for t in targets],
                            widget=forms.Select,
                            label=f"Match for: {premise}",
                            required=True,
                        )
                        self.fields[fname].widget.attrs["class"] = "form-control"
                    item_fields.append((item_key, [f"{base}_match_{i}" for i in range(len(premises))]))
                    continue
                else:
                    self.fields[base] = forms.CharField(
                        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
                        label=question,
                        required=False,
                    )
                self.fields[base].widget.attrs["class"] = "form-control"
                item_fields.append((item_key, base))
            self.groups.append((part, item_fields))
            self.answers[part.pk] = {k: f for k, f in item_fields}

    def response_data_for(self, part):
        """Rebuild the student's answer dict for one part from cleaned data."""
        data = {}
        for item_key, field_names in self.answers.get(part.pk, {}).items():
            if part.question_type == part.TYPE_MATCHING:
                premises = part.items.get(item_key, {}).get("premises", [])
                answer = {}
                for idx, name in enumerate(field_names):
                    answer[premises[idx]] = self.cleaned_data.get(name, "")
                data[item_key] = answer
            elif part.question_type == part.TYPE_MULTIPLE_CHOICE:
                value = self.cleaned_data.get(field_names)
                data[item_key] = list(value) if isinstance(value, list) else str(value).strip()
            elif part.question_type == part.TYPE_NUMERIC:
                value = self.cleaned_data.get(field_names)
                data[item_key] = float(value) if value is not None else None
            elif part.question_type in (
                part.TYPE_TRUE_FALSE,
                part.TYPE_IDENTIFICATION,
                part.TYPE_SHORT_ANSWER,
            ):
                value = self.cleaned_data.get(field_names)
                data[item_key] = str(value).strip() if value is not None else ""
            else:
                value = self.cleaned_data.get(field_names)
                data[item_key] = value or ""
        return data


class GradeScoreForm(forms.Form):
    """Per-item manual grading fields for subjective responses."""

    def __init__(self, *args, **kwargs):
        responses = kwargs.pop("responses", [])
        super().__init__(*args, **kwargs)
        self.item_fields = []  # (response, item_key, field_name, max_points)
        for resp in responses:
            for item_key, item in resp.part.items.items():
                point = Decimal(str(item.get("point", 0)))
                fname = f"score_{resp.pk}__{item_key}"
                initial = resp.item_scores.get(item_key, {}).get("score")
                self.fields[fname] = forms.DecimalField(
                    max_digits=6,
                    decimal_places=2,
                    min_value=0,
                    max_value=point,
                    label=f"Score for item: {item.get('question', item_key)}",
                    initial=initial,
                )
                self.item_fields.append((resp, item_key, fname, point))
        self.fields["teacher_remarks"] = forms.CharField(
            widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
            label="Overall Remarks",
            required=False,
        )
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def build_item_scores(self):
        """
        Return {resp_pk: {item_key: {"score": Decimal, "feedback": ""}}} from
        cleaned data so the view can apply per-item grading.
        """
        scores = {}
        for resp, item_key, fname, _point in self.item_fields:
            value = self.cleaned_data.get(fname)
            if value is None:
                continue
            scores.setdefault(resp.pk, {})[item_key] = {
                "score": float(value),
                "feedback": "",
            }
        return scores


class GradingComponentForm(forms.Form):
    """
    Multi-component grading formula editor for one virtual classroom.
    Exposes one DecimalField per Assessment.ASSESSMENT_TYPE_CHOICES entry;
    weights must total 100 when the form is saved.
    """

    label_map = dict(Assessment.ASSESSMENT_TYPE_CHOICES)

    def __init__(self, *args, **kwargs):
        classroom = kwargs.pop("classroom", None)
        formula = kwargs.pop("formula", None)
        super().__init__(*args, **kwargs)
        current = {}
        if formula:
            current = {
                c.component_type: c.weight_percentage for c in formula.components.all()
            }
        for value, label in Assessment.ASSESSMENT_TYPE_CHOICES:
            self.fields[f"w_{value}"] = forms.DecimalField(
                max_digits=5,
                decimal_places=2,
                min_value=0,
                max_value=100,
                required=False,
                label=label,
                initial=current.get(value),
                help_text="Weight percentage for this grading component (blank = 0).",
            )
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned = super().clean()
        components = []
        total = Decimal("0")
        for value, _label in Assessment.ASSESSMENT_TYPE_CHOICES:
            weight = cleaned.get(f"w_{value}")
            if weight is not None:
                components.append((value, weight))
                total += weight
        if total != Decimal("100"):
            raise ValidationError(
                f"Included component weights must total exactly 100%. Current total: {total}%."
            )
        cleaned["components"] = components
        return cleaned