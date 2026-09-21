from django.contrib import admin
from .models import GeneratedReport, SFReportTemplate


@admin.register(GeneratedReport)
class GeneratedReportAdmin(admin.ModelAdmin):
    list_display = ("report_type", "scope_description", "academic_year", "generated_by", "generated_at")
    list_filter = ("report_type", "academic_year")


@admin.register(SFReportTemplate)
class SFReportTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "form_type", "is_active", "uploaded_by", "uploaded_at")
    list_filter = ("form_type", "is_active")