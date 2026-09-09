from apps.academic_structure.models import AcademicYear
from django import forms

from .models import RoleAccessReview


class RoleAccessReviewForm(forms.ModelForm):
    class Meta:
        model = RoleAccessReview
        fields = ["academic_year", "notes"]
        widgets = {
            "academic_year": forms.Select(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if hasattr(AcademicYear, "STATUS_ACTIVE"):
            self.fields["academic_year"].queryset = AcademicYear.objects.filter(
                status=AcademicYear.STATUS_ACTIVE
            )
        self.fields["academic_year"].label = "Academic Year"
        self.fields["notes"].help_text = "Optional notes about this role/access review."
