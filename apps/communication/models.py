from django.db import models

# Campus audience for a school-wide notice. The three values mirror
# `apps.accounts.campus`, which is what actually applies them -- the copy here
# exists only so Django can render the label on a ModelForm.
AUDIENCE_WHOLE_SCHOOL, AUDIENCE_ELEMENTARY, AUDIENCE_HIGH_SCHOOL = (
    "whole_school",
    "elementary",
    "high_school",
)
AUDIENCE_CHOICES = [
    (AUDIENCE_WHOLE_SCHOOL, "Whole School"),
    (AUDIENCE_ELEMENTARY, "Elementary"),
    (AUDIENCE_HIGH_SCHOOL, "High School"),
]


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
    # Which campus may read this notice. See the migration docstring for why
    # `posted_by` deliberately does not drive the read filter: the audience is
    # decided once, at post time, and then stands on its own.
    audience = models.CharField(
        max_length=20,
        choices=AUDIENCE_CHOICES,
        default="whole_school",
        blank=True,
    )
    # The author's resolved campus at the moment of posting, kept for audit.
    audience_at_posted_at = models.CharField(
        max_length=20, choices=AUDIENCE_CHOICES, default="", blank=True
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


class SchoolEvent(models.Model):
    """A dated school activity shown on the calendar every dashboard can read.

    The System Administrator and the Principal create and edit these; every
    other role sees the calendar read-only. There is deliberately no recurrence
    rule: one row is one activity, optionally spanning several days.
    """

    CATEGORY_CHOICES = [
        ("event", "School Event"),
        ("academic", "Academic Deadline"),
        ("exam", "Examination"),
        ("meeting", "Meeting"),
        ("holiday", "Holiday"),
        ("activity", "Activity"),
    ]

    STATUS_DRAFT, STATUS_PUBLISHED = "draft", "published"
    STATUS_CHOICES = [
        (STATUS_PUBLISHED, "Published"),
        (STATUS_DRAFT, "Draft"),
    ]

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="event")
    start_date = models.DateField()
    # Set only for a multi-day activity; null means the activity is one day.
    end_date = models.DateField(null=True, blank=True)
    all_day = models.BooleanField(default=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_PUBLISHED)
    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="school_events",
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="school_events_created"
    )
    updated_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="school_events_updated"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_date", "title"]
        indexes = [models.Index(fields=["start_date"])]
        permissions = [
            ("add_school_event", "Add School Event"),
            ("change_school_event", "Change School Event"),
            ("delete_school_event", "Delete School Event"),
        ]

    def clean(self):
        # An end date before the start date would render as a negative-length
        # span on the grid, so reject it rather than silently swapping.
        if self.end_date and self.start_date and self.end_date < self.start_date:
            from django.core.exceptions import ValidationError

            raise ValidationError({"end_date": "End date cannot be earlier than the start date."})
        if not self.all_day and not self.start_time:
            from django.core.exceptions import ValidationError

            raise ValidationError({"start_time": "A timed activity needs a start time."})

    @property
    def spans_multiple_days(self):
        return bool(self.end_date and self.end_date != self.start_date)

    def __str__(self):
        return f"{self.start_date:%Y-%m-%d} {self.title}"


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
