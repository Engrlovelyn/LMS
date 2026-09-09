from django.contrib import admin
from .models import RoleAccessReview


@admin.register(RoleAccessReview)
class RoleAccessReviewAdmin(admin.ModelAdmin):
    list_display = ("academic_year", "conducted_by", "completed_at")