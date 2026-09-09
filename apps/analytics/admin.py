from django.contrib import admin
from .models import GeneratedReport


@admin.register(GeneratedReport)
class GeneratedReportAdmin(admin.ModelAdmin):
    list_display = ("report_type", "scope_description", "academic_year", "generated_by", "generated_at")
    list_filter = ("report_type", "academic_year")