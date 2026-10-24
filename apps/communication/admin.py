from django.contrib import admin
from .models import ClassAnnouncement, GroupMessage, Notification, PrivateMessage, SchoolAnnouncement


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("recipient", "notification_type", "title", "is_read", "created_at")
    list_filter = ("notification_type", "is_read")
    search_fields = ("recipient__email", "title", "message")
    date_hierarchy = "created_at"


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