from django.db import models


class RoleAccessReview(models.Model):
    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear", on_delete=models.PROTECT, related_name="role_access_reviews"
    )
    conducted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="conducted_role_reviews"
    )
    notes = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Snapshot of enabled role groups and their member counts, captured when
    # the review is completed ({"group_name": count, ...}).
    role_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year"],
                name="one_review_per_academic_year",
            )
        ]
        permissions = [
            ("conduct_annual_role_access_review", "Conduct Annual Role/Access Review"),
            ("retrieve_role_access_review_records", "Retrieve Role/Access Review Records"),
        ]

    def __str__(self):
        return f"Role/Access Review - {self.academic_year}"

    @property
    def is_completed(self):
        return self.completed_at is not None
