from django.db import models


class RoleAccessReview(models.Model):
    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear", on_delete=models.PROTECT, related_name="role_access_reviews"
    )
    conducted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="conducted_role_reviews"
    )
    notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year"],
                name="one_review_per_academic_year",
            )
        ]
        permissions = [
            ("conduct_annual_role_access_review", "Conduct Annual Role/Access Review"),
        ]

    def __str__(self):
        return f"Role/Access Review - {self.academic_year}"
