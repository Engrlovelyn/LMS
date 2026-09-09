from django.db import models
from django.core.exceptions import ValidationError


class TopicTemplate(models.Model):
    subject = models.ForeignKey(
        "academic_structure.Subject", on_delete=models.CASCADE, related_name="topic_templates"
    )
    name = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "order"],
                name="unique_topic_template_order_per_subject",
            )
        ]

    def __str__(self):
        return f"{self.subject} - {self.name}"


class Topic(models.Model):
    virtual_classroom = models.ForeignKey(
        "virtual_classrooms.VirtualClassroom", on_delete=models.CASCADE, related_name="topics"
    )
    name = models.CharField(max_length=200)
    order = models.PositiveIntegerField(default=0)
    is_supplementary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order"]
        permissions = [
            ("add_topic_custom", "Add Topic"),
        ]

    def __str__(self):
        return f"{self.virtual_classroom} - {self.name}"


class LearningResource(models.Model):
    STATUS_ACTIVE, STATUS_ARCHIVED = "active", "archived"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    TYPE_DOCUMENT = "document"
    TYPE_PDF = "pdf"
    TYPE_PRESENTATION = "presentation"
    TYPE_IMAGE = "image"
    TYPE_VIDEO = "video"
    TYPE_AUDIO = "audio"
    TYPE_LINK = "link"
    RESOURCE_TYPE_CHOICES = [
        (TYPE_DOCUMENT, "Document"),
        (TYPE_PDF, "PDF"),
        (TYPE_PRESENTATION, "Presentation"),
        (TYPE_IMAGE, "Image"),
        (TYPE_VIDEO, "Video"),
        (TYPE_AUDIO, "Audio"),
        (TYPE_LINK, "External Link"),
    ]

    subject = models.ForeignKey(
        "academic_structure.Subject", on_delete=models.PROTECT, related_name="learning_resources"
    )
    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear", on_delete=models.PROTECT, related_name="learning_resources"
    )
    uploaded_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="uploaded_resources"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    resource_type = models.CharField(max_length=20, choices=RESOURCE_TYPE_CHOICES)
    file = models.FileField(upload_to="learning_resources/", null=True, blank=True)
    external_link = models.URLField(null=True, blank=True)
    available_from = models.DateField(null=True, blank=True)
    available_until = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if not self.file and not self.external_link:
            raise ValidationError("Provide either a file or an external link.")
        if self.file and self.external_link:
            raise ValidationError("Provide only one: a file OR an external link, not both.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    class Meta:
        permissions = [
            ("upload_learning_resource", "Upload Learning Resource"),
            ("access_subject_level_repository", "Access Subject-Level Resource Repository"),
            ("manage_learning_resources", "Manage Learning Resources"),
            ("view_download_learning_resource", "View and Download Learning Resource"),
        ]


class LearningResourcePlacement(models.Model):
    resource = models.ForeignKey(LearningResource, on_delete=models.CASCADE, related_name="placements")
    virtual_classroom = models.ForeignKey(
        "virtual_classrooms.VirtualClassroom", on_delete=models.CASCADE, related_name="resource_placements"
    )
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name="resource_placements")
    placed_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="resource_placements"
    )
    is_original = models.BooleanField(default=True)
    placed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "virtual_classroom", "topic"],
                name="unique_resource_placement",
            )
        ]

    def __str__(self):
        return f"{self.resource} -> {self.virtual_classroom} / {self.topic}"
