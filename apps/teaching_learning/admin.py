from django.contrib import admin
from .models import TopicTemplate, Topic, LearningResource, LearningResourcePlacement


@admin.register(TopicTemplate)
class TopicTemplateAdmin(admin.ModelAdmin):
    list_display = ("subject", "name", "order")
    list_filter = ("subject",)
    ordering = ("subject", "order")


class TopicInline(admin.TabularInline):
    model = Topic
    extra = 0


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("virtual_classroom", "name", "order", "is_supplementary")
    list_filter = ("virtual_classroom", "is_supplementary")


@admin.register(LearningResource)
class LearningResourceAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "academic_year", "resource_type", "uploaded_by", "status")
    list_filter = ("subject", "academic_year", "resource_type", "status")
    search_fields = ("title",)


@admin.register(LearningResourcePlacement)
class LearningResourcePlacementAdmin(admin.ModelAdmin):
    list_display = ("resource", "virtual_classroom", "topic", "placed_by", "is_original", "placed_at")
    list_filter = ("virtual_classroom", "is_original")