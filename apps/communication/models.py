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

# Role-based audience. This is deliberately separate from the campus audience
# above: a notice can be campus-scoped *and* addressed to students or teachers.
# "all" keeps every older announcement behaving exactly as it did before the
# column existed (the migration backfills existing rows to "all").
TARGET_ALL = "all"
TARGET_STUDENTS = "students"
TARGET_TEACHERS = "teachers"
TARGET_ROLE_CHOICES = [
    (TARGET_ALL, "Everyone"),
    (TARGET_STUDENTS, "Students Only"),
    (TARGET_TEACHERS, "Teachers Only"),
]


def user_matches_target_role(user, target_role):
    """True when ``user`` belongs to the role an announcement targets.

    Administrators are treated as able to read every notice; the restriction is
    aimed at students and teachers, not at the office that publishes it.
    """
    target_role = target_role or TARGET_ALL
    if target_role == TARGET_ALL:
        return True
    is_teacher = bool(user and user.is_authenticated and user.groups.filter(name="teacher").exists())
    is_student = bool(
        user and user.is_authenticated and getattr(user, "student_profile", None) is not None
    )
    if user and user.is_authenticated and (user.is_superuser or user.is_staff):
        return True
    if target_role == TARGET_STUDENTS:
        return is_student
    if target_role == TARGET_TEACHERS:
        return is_teacher
    return True


class Notification(models.Model):
    """A persisted, per-user notification shown by the header bell.

    This is the durable counterpart to the ephemeral realtime pings in
    ``communication.realtime``: a realtime event tells an open dashboard to
    repaint, while a ``Notification`` row is what makes the bell keep a red dot
    and a readable list until *that* recipient opens it. Read state is stored on
    the row itself, so it is per student and survives a reload or a new device.

    The row deliberately does not hold a generic foreign key to the object that
    caused it (an assessment, a material placement, an announcement). Because
    the notification must be class-scoped and re-checked on open, a plain
    ``(source_type, source_id)`` pair plus an optional ``virtual_classroom`` is
    enough to resolve access at click time without importing those apps here and
    creating a cycle -- ``assessments`` already imports ``communication``.
    """

    TYPE_ANNOUNCEMENT = "announcement"
    TYPE_MATERIAL = "material"
    TYPE_ASSESSMENT = "assessment"
    TYPE_ATTENDANCE = "attendance"
    TYPE_CHOICES = [
        (TYPE_ANNOUNCEMENT, "Class Announcement"),
        (TYPE_MATERIAL, "Learning Material"),
        (TYPE_ASSESSMENT, "Assessment"),
        (TYPE_ATTENDANCE, "Attendance"),
    ]

    recipient = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="notifications"
    )
    virtual_classroom = models.ForeignKey(
        "virtual_classrooms.VirtualClassroom",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="notifications",
    )
    notification_type = models.CharField(max_length=20, choices=TYPE_CHOICES)

    # Origin of the notification, e.g. ("class_announcement", 12) or
    # ("assessment", 4). ``source_type`` is a loose string rather than a choice
    # list so a new producer does not need a migration to be added.
    source_type = models.CharField(max_length=40, blank=True)
    source_id = models.PositiveIntegerField(null=True, blank=True)

    title = models.CharField(max_length=200)
    message = models.TextField(blank=True)
    # Where opening the notification should take the recipient. Re-checked for
    # access at click time; a recipient who has lost access is sent to the list.
    url = models.CharField(max_length=500, blank=True)

    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One notification per recipient per source object. A teacher
            # editing an announcement, or re-running a notification sweep, must
            # not multiply the bell's red dot.
            models.UniqueConstraint(
                fields=["recipient", "notification_type", "source_type", "source_id"],
                name="uniq_notification_per_recipient_source",
            ),
        ]
        indexes = [
            models.Index(fields=["recipient", "is_read"]),
            models.Index(fields=["recipient", "created_at"]),
        ]

    def __str__(self):
        return f"{self.recipient} <- {self.notification_type}:{self.source_type}#{self.source_id}"


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
    target_role = models.CharField(
        max_length=10,
        choices=TARGET_ROLE_CHOICES,
        default=TARGET_ALL,
        blank=True,
        help_text="Who in the classroom may read this announcement.",
    )
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
    target_role = models.CharField(
        max_length=10,
        choices=TARGET_ROLE_CHOICES,
        default=TARGET_ALL,
        blank=True,
        help_text="Students Only, Teachers Only, or Everyone.",
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
