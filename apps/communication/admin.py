from django.contrib import admin
from .models import ClassAnnouncement, SchoolAnnouncement, GroupMessage, PrivateMessage


@admin.register(ClassAnnouncement)
class ClassAnnouncementAdmin(admin.ModelAdmin):
    list_display = ("virtual_classroom", "posted_by", "posted_at", "status")
    list_filter = ("virtual_classroom", "status")


@admin.register(SchoolAnnouncement)
class SchoolAnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "posted_by", "posted_at", "status")
    list_filter = ("status",)
    search_fields = ("title",)


@admin.register(GroupMessage)
class GroupMessageAdmin(admin.ModelAdmin):
    list_display = ("virtual_classroom", "sender", "sent_at")
    list_filter = ("virtual_classroom",)


@admin.register(PrivateMessage)
class PrivateMessageAdmin(admin.ModelAdmin):
    list_display = ("sender", "recipient", "sent_at", "read_at")
    search_fields = ("sender__email", "recipient__email")