from django.db import models


class ClassAnnouncement(models.Model):
    STATUS_ACTIVE, STATUS_ARCHIVED = "active", "archived"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    virtual_classroom = models.ForeignKey(
        "virtual_classrooms.VirtualClassroom", on_delete=models.CASCADE, related_name="announcements"
    )
    posted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="class_announcements"
    )
    content = models.TextField()
    attachment = models.FileField(upload_to="announcement_attachments/", null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    posted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-posted_at"]
        permissions = [
            ("post_class_announcement", "Post Class Announcement"),
            ("view_announcements_custom", "View Announcements"),
            ("update_class_announcement", "Update Class Announcement"),
            ("archive_class_announcement", "Archive Class Announcement"),
            ("send_group_messaging", "Send Group Messaging"),
            ("send_private_message", "Send Private Message"),
        ]

    def __str__(self):
        return f"{self.virtual_classroom} - {self.content[:40]}"


class SchoolAnnouncement(models.Model):
    STATUS_ACTIVE, STATUS_ARCHIVED = "active", "archived"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    title = models.CharField(max_length=200)
    content = models.TextField()
    posted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="school_announcements"
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    posted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-posted_at"]
        permissions = [
            ("post_school_wide_announcement", "Post School-Wide Announcement"),
            ("update_school_wide_announcement", "Update School-Wide Announcement"),
            ("archive_school_wide_announcement", "Archive School-Wide Announcement"),
        ]

    def __str__(self):
        return self.title


class GroupMessage(models.Model):
    virtual_classroom = models.ForeignKey(
        "virtual_classrooms.VirtualClassroom", on_delete=models.CASCADE, related_name="group_messages"
    )
    sender = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="sent_group_messages")
    content = models.TextField(blank=True)
    attachment = models.FileField(upload_to="message_attachments/", null=True, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sent_at"]

    def __str__(self):
        return f"{self.sender} in {self.virtual_classroom} @ {self.sent_at:%Y-%m-%d %H:%M}"


class PrivateMessage(models.Model):
    sender = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="sent_private_messages"
    )
    recipient = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="received_private_messages"
    )
    content = models.TextField(blank=True)
    attachment = models.FileField(upload_to="message_attachments/", null=True, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sent_at"]

    def __str__(self):
        return f"{self.sender} -> {self.recipient} @ {self.sent_at:%Y-%m-%d %H:%M}"
