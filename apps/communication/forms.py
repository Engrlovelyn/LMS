from django import forms

from .models import ClassAnnouncement, GroupMessage, SchoolAnnouncement


class SchoolAnnouncementForm(forms.ModelForm):
    class Meta:
        model = SchoolAnnouncement
        fields = ["title", "content", "status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"


class ClassAnnouncementForm(forms.ModelForm):
    class Meta:
        model = ClassAnnouncement
        fields = ["virtual_classroom", "content", "attachment", "status"]

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        self.fields["virtual_classroom"].widget.attrs["class"] = "form-control"
        self.fields["content"].widget.attrs["class"] = "form-control"
        self.fields["attachment"].widget.attrs["class"] = "form-control"
        self.fields["status"].widget.attrs["class"] = "form-control"
        if user is not None:
            from apps.virtual_classrooms.models import VirtualClassroom

            queryset = VirtualClassroom.objects.all()
            if not user.is_superuser:
                queryset = queryset.filter(teacher=user)
            self.fields["virtual_classroom"].queryset = queryset


class GroupMessageForm(forms.ModelForm):
    class Meta:
        model = GroupMessage
        fields = ["content", "attachment"]
        widgets = {"content": forms.Textarea(attrs={"rows": 3, "placeholder": "Type a message..."})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["content"].widget.attrs["class"] = "form-control"
        self.fields["attachment"].widget.attrs["class"] = "form-control"


class PrivateMessageForm(forms.Form):
    content = forms.CharField(widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Type a message..."}))
    attachment = forms.FileField(required=False, widget=forms.ClearableFileInput(attrs={"class": "form-control"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["content"].widget.attrs["class"] = "form-control"
