from django import forms

from .models import LearningResource, Topic


class LearningResourceForm(forms.ModelForm):
    class Meta:
        model = LearningResource
        fields = [
            "subject",
            "title",
            "resource_type",
            "description",
            "external_link",
            "file",
            "available_from",
            "available_until",
            "share_to_repository",
            "status",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
        self.fields["share_to_repository"].label = "Share with subject repository"
        self.fields["share_to_repository"].help_text = (
            "Leave unchecked to keep this resource in its Virtual Classroom only. "
            "When checked, teachers who teach this same subject can find and reuse it."
        )


class TopicForm(forms.ModelForm):
    class Meta:
        model = Topic
        fields = ["name", "is_supplementary", "order"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if field.widget.__class__.__name__ in ("CheckboxInput", "CheckboxSelectMultiple"):
                field.widget.attrs["class"] = "form-check-input"
            else:
                field.widget.attrs["class"] = "form-control"


class ResourceAttachForm(forms.Form):
    resources = forms.ModelMultipleChoiceField(
        queryset=LearningResource.objects.all(),
        widget=forms.CheckboxSelectMultiple,
        label="Select resources",
    )
    topic = forms.ModelChoiceField(
        queryset=Topic.objects.none(),
        empty_label="Select a topic...",
        label="Place under topic",
        required=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["topic"].widget.attrs["class"] = "form-control"
