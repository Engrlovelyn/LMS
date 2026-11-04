from decimal import Decimal

from django.db import models
from django.core.exceptions import ValidationError
from django.utils import timezone


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------
class Assessment(models.Model):
    """
    One assessment (quiz, seatwork, assignment, examination, or project).

    - Rule (a): concrete requirement format is captured in `assessment_method`.
    - Rule (b): lifecycle is tracked explicitly via `lifecycle_stage`.
    - Rule (c)/(d): an assessment is composed of one or more `AssessmentPart`
      records, each independently objective or subjective (see AssessmentPart).
    """

    # --- (a) Assessment method: the concrete requirement format -----------
    METHOD_QUIZ = "quiz"
    METHOD_SEATWORK = "seatwork"
    METHOD_ASSIGNMENT = "assignment"
    METHOD_EXAMINATION = "examination"
    METHOD_PROJECT = "project"
    ASSESSMENT_METHOD_CHOICES = [
        (METHOD_QUIZ, "Quiz"),
        (METHOD_SEATWORK, "Seatwork"),
        (METHOD_ASSIGNMENT, "Assignment"),
        (METHOD_EXAMINATION, "Examination"),
        (METHOD_PROJECT, "Project"),
    ]

    # --- Grading-component classification (feeds GradingComponent weights) -
    TYPE_WRITTEN_ORAL = "written_oral"
    TYPE_PRODUCT_PERFORMANCE = "product_performance"
    TYPE_SUMMATIVE_TEST = "summative_test"
    TYPE_TERM_EXAM = "term_exam"
    ASSESSMENT_TYPE_CHOICES = [
        (TYPE_WRITTEN_ORAL, "Written/Oral Work"),
        (TYPE_PRODUCT_PERFORMANCE, "Product/Performance Task"),
        (TYPE_SUMMATIVE_TEST, "Examination - Summative Test"),
        (TYPE_TERM_EXAM, "Examination - Term Examination"),
    ]

    # --- (b) Lifecycle: creation -> availability -> submission -> grading
    #         -> completion --------------------------------------------------
    STAGE_CREATED = "created"
    STAGE_AVAILABLE = "available"
    STAGE_SUBMISSION = "submission"
    STAGE_GRADING = "grading"
    STAGE_COMPLETED = "completed"
    LIFECYCLE_STAGE_CHOICES = [
        (STAGE_CREATED, "Created"),
        (STAGE_AVAILABLE, "Available"),
        (STAGE_SUBMISSION, "Submission"),
        (STAGE_GRADING, "Grading"),
        (STAGE_COMPLETED, "Completed"),
    ]

    STATUS_ACTIVE, STATUS_ARCHIVED = "active", "archived"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    ]

    subject = models.ForeignKey(
        "academic_structure.Subject", on_delete=models.PROTECT, related_name="assessments"
    )
    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear", on_delete=models.PROTECT, related_name="assessments"
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="created_assessments"
    )
    title = models.CharField(max_length=200)

    assessment_method = models.CharField(
        max_length=20,
        choices=ASSESSMENT_METHOD_CHOICES,
        help_text="Concrete requirement format, e.g. quiz, seatwork, assignment, examination, project.",
    )
    assessment_type = models.CharField(
        max_length=30,
        choices=ASSESSMENT_TYPE_CHOICES,
        help_text="Grading-component classification used to weight scores in GradingFormula.",
    )

    instructions = models.TextField(blank=True)

    # --- (b) lifecycle tracking ---------------------------------------------
    lifecycle_stage = models.CharField(
        max_length=15, choices=LIFECYCLE_STAGE_CHOICES, default=STAGE_CREATED
    )
    available_at = models.DateTimeField(
        null=True, blank=True, help_text="When the assessment becomes visible/available to students."
    )
    submission_deadline = models.DateTimeField()
    accepted_file_format = models.CharField(max_length=100, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)

    # --- (j) notification configuration -------------------------------------
    # JSON because the reminder schedule/shape can vary per assessment
    # without requiring schema changes.
    # Example: {
    #   "notify_on_availability": true,
    #   "deadline_reminder_hours_before": [72, 24, 1],
    #   "notify_on_missed": true
    # }
    notification_settings = models.JSONField(default=dict, blank=True)

    class Meta:
        permissions = [
            ("create_assessment_custom", "Create Assessment"),
            ("access_assessment_repository", "Access Assessment Repository"),
            ("view_assessment_custom", "View Assessment"),
            ("update_assessment_custom", "Update Assessment"),
            ("complete_assessment_custom", "Complete Assessment"),
            ("monitor_student_submissions", "Monitor Student Submissions"),
            ("manually_grade_subjective_assessment", "Manually Grade Subjective Assessment"),
            ("archive_assessment_custom", "Archive Assessment"),
            ("trigger_missed_assessment_notification", "Trigger Missed Assessment Notification"),
            ("auto_grade_objective_assessment", "Auto-Grade Objective Assessment"),
            ("compute_student_grade", "Compute Student Grade"),
            ("view_student_performance_tracking", "View Student Performance Tracking"),
            ("view_grade_book", "View Grade Book"),
        ]

    def __str__(self):
        return self.title

    @property
    def total_possible_score(self):
        """(h) Sum of points across all parts, objective and subjective alike."""
        return sum((part.points for part in self.parts.all()), start=0)

    @property
    def has_subjective_parts(self):
        return self.parts.filter(question_type__in=AssessmentPart.SUBJECTIVE_TYPES).exists()


# ---------------------------------------------------------------------------
# AssessmentPart
# ---------------------------------------------------------------------------
class AssessmentPart(models.Model):
    """
    A single component of an assessment (Rule d) — an assessment has one or
    more parts, and each part has its own distinct `question_type`.

    Every part compiles ALL of its actual questions into one `items` JSON
    column, keyed by item id ("item_1", "item_2", ...). The shape of each
    item depends on `question_type`:

    multiple_choice / true_false:
        {
          "question": "What is the capital of France?",
          "choices": ["Berlin", "London", "Paris", "Madrid"],
          "correct_answer": "Paris",
          "point": 1
        }

    matching_type:
        {
          "question": "Match the country with its capital city.",
          "type": "matching",
          "premises": ["France", "Japan", "Italy", "Canada"],
          "targets": ["Tokyo", "Rome", "Paris", "Ottawa"],
          "correct_matches": {"France": "Paris", "Japan": "Tokyo", "Italy": "Rome", "Canada": "Ottawa"},
          "point": 4
        }

    identification / short_answer:
        {
          "question": "What is the term for the process by which plants make their own food using sunlight?",
          "type": "identification",
          "accepted_answers": ["Photosynthesis", "photosynthesis"],
          "point": 1
        }

    numeric:
        {
          "question": "What is 12 x 8?",
          "correct_answer": 96,
          "tolerance": 0,
          "point": 1
        }

    essay / position_paper / case_study / narrative_report
    (subjective — Rule f, manually graded):
        {
          "question": "Discuss the potential impacts of artificial intelligence on the future of education.",
          "type": "essay",
          "min_word_count": 150,
          "max_word_count": 500,
          "grading_rubric": [
            "Clarity and structure of the argument",
            "Inclusion of both positive and negative perspectives",
            "Relevance of examples provided",
            "Grammar and vocabulary usage"
          ],
          "point": 10
        }

    (e) Objective types are auto-scored per item against that item's own
    answer key. (f) Subjective types always require manual, per-item grading
    by the assigned teacher against `grading_rubric`.
    """

    TYPE_SHORT_ANSWER = "short_answer"
    TYPE_IDENTIFICATION = "identification"
    TYPE_TRUE_FALSE = "true_false"
    TYPE_MULTIPLE_CHOICE = "multiple_choice"
    TYPE_MATCHING = "matching_type"
    TYPE_NUMERIC = "numeric"
    TYPE_ESSAY = "essay"
    TYPE_POSITION_PAPER = "position_paper"
    TYPE_CASE_STUDY = "case_study"
    TYPE_NARRATIVE_REPORT = "narrative_report"

    QUESTION_TYPE_CHOICES = [
        (TYPE_SHORT_ANSWER, "Short Answer"),
        (TYPE_IDENTIFICATION, "Identification"),
        (TYPE_TRUE_FALSE, "True/False"),
        (TYPE_MULTIPLE_CHOICE, "Multiple Choice"),
        (TYPE_MATCHING, "Matching Type"),
        (TYPE_NUMERIC, "Numeric"),
        (TYPE_ESSAY, "Essay"),
        (TYPE_POSITION_PAPER, "Position Paper"),
        (TYPE_CASE_STUDY, "Case Study"),
        (TYPE_NARRATIVE_REPORT, "Narrative Report"),
    ]

    # (e) Objective: each item is auto-evaluated against its own answer key.
    OBJECTIVE_TYPES = [
        TYPE_SHORT_ANSWER,
        TYPE_IDENTIFICATION,
        TYPE_TRUE_FALSE,
        TYPE_MULTIPLE_CHOICE,
        TYPE_MATCHING,
        TYPE_NUMERIC,
    ]

    # (f) Subjective/open-ended: every item always requires manual grading.
    SUBJECTIVE_TYPES = [
        TYPE_ESSAY,
        TYPE_POSITION_PAPER,
        TYPE_CASE_STUDY,
        TYPE_NARRATIVE_REPORT,
    ]

    # Keys every item of a given type must have; used by clean() to validate
    # the compiled `items` bank before an assessment can be published.
    _REQUIRED_ITEM_KEYS = {
        TYPE_SHORT_ANSWER: ("question", "accepted_answers", "point"),
        TYPE_IDENTIFICATION: ("question", "accepted_answers", "point"),
        TYPE_TRUE_FALSE: ("question", "choices", "correct_answer", "point"),
        TYPE_MULTIPLE_CHOICE: ("question", "choices", "correct_answer", "point"),
        TYPE_MATCHING: ("question", "premises", "targets", "correct_matches", "point"),
        TYPE_NUMERIC: ("question", "correct_answer", "point"),
        TYPE_ESSAY: ("question", "point"),
        TYPE_POSITION_PAPER: ("question", "point"),
        TYPE_CASE_STUDY: ("question", "point"),
        TYPE_NARRATIVE_REPORT: ("question", "point"),
    }

    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE, related_name="parts")
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES)

    # Section-level label/instructions (e.g. "Part I - Multiple Choice").
    # The actual questions live inside each item's own "question" key.
    question_text = models.TextField(blank=True)

    order = models.PositiveIntegerField(default=0)

    # (d)/(e)/(f) The full compiled bank of items for this part.
    # See the class docstring above for the exact shape per question_type.
    items = models.JSONField(default=dict, blank=True)

    # Part-level total points — always derived automatically as the sum of
    # every item's "point" value (see save()); never set directly.
    points = models.DecimalField(max_digits=6, decimal_places=2, default=0, editable=False)

    attached_file = models.FileField(upload_to="assessment_parts/", null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]

    @property
    def is_objective(self):
        return self.question_type in self.OBJECTIVE_TYPES

    @property
    def requires_manual_grading(self):
        """(f) Subjective/open-ended parts always require teacher grading."""
        return self.question_type in self.SUBJECTIVE_TYPES

    def clean(self):
        if not self.items:
            raise ValidationError("An assessment part must define at least one item.")
        required_keys = self._REQUIRED_ITEM_KEYS.get(self.question_type, ("question", "point"))
        for item_id, item in self.items.items():
            missing = [key for key in required_keys if key not in item]
            if missing:
                raise ValidationError(
                    f"Item '{item_id}' is missing required field(s): {', '.join(missing)}."
                )
            if self.question_type == self.TYPE_MATCHING:
                unmatched = set(item.get("correct_matches", {})) - set(item.get("premises", []))
                if unmatched:
                    raise ValidationError(
                        f"Item '{item_id}' has correct_matches not listed in premises: {sorted(unmatched)}."
                    )

    def save(self, *args, **kwargs):
        # (h) Points always reflect the compiled items, for every type.
        self.points = sum(
            (Decimal(str(item.get("point", 0))) for item in self.items.values()),
            start=Decimal("0"),
        )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.assessment} - {self.question_text[:50] or self.get_question_type_display()}"


# ---------------------------------------------------------------------------
# Assignment to virtual classrooms
# ---------------------------------------------------------------------------
class AssessmentAssignment(models.Model):
    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE, related_name="assignments")
    virtual_classroom = models.ForeignKey(
        "virtual_classrooms.VirtualClassroom", on_delete=models.CASCADE, related_name="assessment_assignments"
    )
    assigned_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="assessment_assignments"
    )
    is_original = models.BooleanField(default=True)
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assessment", "virtual_classroom"],
                name="unique_assessment_per_vc",
            )
        ]

    def __str__(self):
        return f"{self.assessment} -> {self.virtual_classroom}"


class AssessmentAttempt(models.Model):
    """One server-timed working attempt, separate from a final submission."""

    assignment = models.ForeignKey(
        AssessmentAssignment, on_delete=models.CASCADE, related_name="attempts"
    )
    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.CASCADE, related_name="assessment_attempts"
    )
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    answers = models.JSONField(default=dict, blank=True)
    revision = models.PositiveIntegerField(default=0)
    last_saved_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assignment", "student"], name="unique_assessment_attempt_per_student"
            )
        ]

    def __str__(self):
        return f"Attempt {self.pk}: {self.student} - {self.assignment}"


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------
class Submission(models.Model):
    """
    A student's submission for an assessment.

    Rules (g)/(h)/(i): `total_score` only reflects components that are fully
    graded. Objective parts are scored immediately; subjective parts count
    only once their manual grading is completed, which is why the submission
    tracks its own `grading_status` separate from individual response scores.
    """

    GRADING_PENDING = "pending"
    GRADING_PARTIAL = "partially_graded"
    GRADING_COMPLETE = "grading_complete"
    GRADING_STATUS_CHOICES = [
        (GRADING_PENDING, "Pending"),
        (GRADING_PARTIAL, "Partially Graded"),
        (GRADING_COMPLETE, "Grading Complete"),
    ]

    assessment_assignment = models.ForeignKey(
        AssessmentAssignment, on_delete=models.PROTECT, related_name="submissions"
    )
    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.PROTECT, related_name="submissions")
    submitted_at = models.DateTimeField(auto_now_add=True)
    is_late = models.BooleanField(default=False)

    # The student's uploaded work for the whole assessment. Stored here (rather
    # than only on a SubmissionResponse) so a file is still kept for
    # assessments that have no parts.
    attached_file = models.FileField(upload_to="submissions/", null=True, blank=True)

    # (g)/(h)/(i) total_score is an aggregate that is only finalized when
    # grading_status == GRADING_COMPLETE.
    total_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    grading_status = models.CharField(
        max_length=20, choices=GRADING_STATUS_CHOICES, default=GRADING_PENDING
    )
    graded_at = models.DateTimeField(null=True, blank=True)
    teacher_remarks = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["assessment_assignment", "student"],
                name="unique_submission_per_student",
            )
        ]

    def save(self, *args, **kwargs):
        if not self.pk:
            deadline = self.assessment_assignment.assessment.submission_deadline
            submitted = self.submitted_at or timezone.now()
            self.is_late = bool(deadline) and submitted > deadline
        super().save(*args, **kwargs)

    def recompute_total_score(self):
        """
        (h)/(i) Aggregate scores from all responses. Subjective responses
        only contribute once their own grading is complete; if any
        subjective response is still pending, the submission as a whole
        stays out of GRADING_COMPLETE and total_score reflects only what
        has been finalized so far.
        """
        responses = self.responses.select_related("part").all()
        pending_manual = any(
            r.part.requires_manual_grading and r.grading_status != SubmissionResponse.GRADED
            for r in responses
        )
        total = sum(
            (r.score for r in responses if r.score is not None),
            start=0,
        )
        self.total_score = total
        if pending_manual:
            has_any_graded = any(r.score is not None for r in responses)
            self.grading_status = self.GRADING_PARTIAL if has_any_graded else self.GRADING_PENDING
        else:
            self.grading_status = self.GRADING_COMPLETE
        self.save(update_fields=["total_score", "grading_status"])

    def __str__(self):
        return f"{self.student} - {self.assessment_assignment.assessment}"


class SubmissionResponse(models.Model):
    """
    A student's answers for one AssessmentPart within a Submission.

    Since a part can bundle many items (Rule d), a response stores the
    student's answer and score per item rather than a single flat
    answer/score for the whole part.
    """

    PENDING = "pending"
    AUTO_GRADED = "auto_graded"
    GRADED = "graded"
    GRADING_STATUS_CHOICES = [
        (PENDING, "Pending"),
        (AUTO_GRADED, "Auto-Graded"),
        (GRADED, "Manually Graded"),
    ]

    submission = models.ForeignKey(Submission, on_delete=models.CASCADE, related_name="responses")
    part = models.ForeignKey(AssessmentPart, on_delete=models.PROTECT, related_name="responses")

    # The student's answer per item, keyed by the same item id used in
    # AssessmentPart.items. Shape mirrors that item's answer-key field:
    #   multiple choice, true_false          -> {"item_1": "Paris"}
    #   matching_type                        -> {"item_1": {"France": "Paris", ...}}
    #   identification/short_answer/numeric  -> {"item_1": "Mars"}
    #   essay-type (subjective)              -> {"item_1": "<student's written answer>"}
    response_data = models.JSONField(default=dict, blank=True)

    # Optional single attachment for the whole part (e.g. supporting
    # material submitted alongside a written response).
    response_file = models.FileField(upload_to="submission_responses/", null=True, blank=True)

    # (e)/(g) Per-item results, keyed the same way as `items`/`response_data`:
    #   {"item_1": {"score": 1, "is_correct": true}, "item_2": {"score": 8, "feedback": "..."}}
    # Objective items are populated automatically by compute_auto_score().
    # Subjective items must be populated manually by the assigned teacher.
    item_scores = models.JSONField(default=dict, blank=True)

    # Aggregate for the whole part — always the sum of item_scores' "score"
    # values. Only counts toward Submission.total_score once grading_status
    # confirms every item (including subjective ones) has actually been scored.
    score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    # (g) A manually graded response only "counts" once grading_status == GRADED.
    grading_status = models.CharField(
        max_length=15, choices=GRADING_STATUS_CHOICES, default=PENDING
    )
    graded_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="graded_responses"
    )
    graded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "part"],
                name="unique_response_per_part",
            )
        ]

    def compute_auto_score(self):
        """
        (e) Auto-grade every item of an objective part by comparing
        `response_data` against the item's own answer key in `part.items`.
        No-op for subjective parts — those must go through manual grading.
        """
        if self.part.requires_manual_grading:
            return
        for item_id, item in self.part.items.items():
            student_answer = self.response_data.get(item_id)
            point = Decimal(str(item.get("point", 0)))
            is_correct = self._item_is_correct(item, student_answer)
            self.item_scores[item_id] = {
                "score": float(point) if is_correct else 0,
                "is_correct": is_correct,
            }
        self.grading_status = self.AUTO_GRADED
        self._recompute_score(save=False)
        if self.pk:
            self.save(update_fields=["item_scores", "score", "grading_status"])
        else:
            self.save()

    @staticmethod
    def _item_is_correct(item, student_answer):
        if student_answer is None:
            return False
        if "correct_matches" in item:  # matching_type
            return student_answer == item["correct_matches"]
        if "accepted_answers" in item:  # identification / short_answer
            accepted = {str(a).strip().lower() for a in item["accepted_answers"]}
            return str(student_answer).strip().lower() in accepted
        if "correct_answer" in item:  # multiple choice, true_false, numeric
            correct = item["correct_answer"]
            if isinstance(correct, list):
                return set(str(x) for x in (student_answer or [])) == set(str(x) for x in correct)
            return str(student_answer).strip().lower() == str(correct).strip().lower()
        return False

    def _recompute_score(self, save=True):
        self.score = sum(
            (Decimal(str(v.get("score", 0))) for v in self.item_scores.values()),
            start=Decimal("0"),
        )
        if save:
            self.save(update_fields=["score"])

    def __str__(self):
        return f"{self.submission} - {self.part}"


# ---------------------------------------------------------------------------
# Grading formula / components
# ---------------------------------------------------------------------------
class GradingFormula(models.Model):
    virtual_classroom = models.OneToOneField(
        "virtual_classrooms.VirtualClassroom", on_delete=models.CASCADE, related_name="grading_formula"
    )
    structure_version = models.ForeignKey(
        "academic_structure.AcademicStructureVersion", on_delete=models.PROTECT, related_name="grading_formulas"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Grading formula - {self.virtual_classroom}"


class GradingComponent(models.Model):
    formula = models.ForeignKey(GradingFormula, on_delete=models.CASCADE, related_name="components")
    component_type = models.CharField(max_length=30, choices=Assessment.ASSESSMENT_TYPE_CHOICES)
    weight_percentage = models.DecimalField(max_digits=5, decimal_places=2)

    def __str__(self):
        return f"{self.formula} - {self.component_type} ({self.weight_percentage}%)"


class StudentGrade(models.Model):
    student = models.ForeignKey("accounts.StudentProfile", on_delete=models.PROTECT, related_name="grades")
    subject = models.ForeignKey(
        "academic_structure.Subject", on_delete=models.PROTECT, related_name="student_grades"
    )
    academic_year = models.ForeignKey(
        "academic_structure.AcademicYear", on_delete=models.PROTECT, related_name="student_grades"
    )
    structure_version = models.ForeignKey(
        "academic_structure.AcademicStructureVersion", on_delete=models.PROTECT, related_name="student_grades"
    )
    grading_period = models.CharField(max_length=20, blank=True)
    grade_value = models.DecimalField(max_digits=5, decimal_places=2)
    computed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "subject", "academic_year", "grading_period"],
                name="unique_student_grade_per_period",
            )
        ]

    def __str__(self):
        return f"{self.student} - {self.subject} ({self.grading_period}): {self.grade_value}"


# ---------------------------------------------------------------------------
# (j) Student notifications
# ---------------------------------------------------------------------------
class AssessmentNotification(models.Model):
    """
    Notifies a student about a key assessment event: availability, an
    upcoming deadline, a nearing deadline, or a missed assessment.
    """

    EVENT_AVAILABLE = "available"
    EVENT_DEADLINE_UPCOMING = "deadline_upcoming"
    EVENT_DEADLINE_NEARING = "deadline_nearing"
    EVENT_MISSED = "missed"
    EVENT_TYPE_CHOICES = [
        (EVENT_AVAILABLE, "Assessment Available"),
        (EVENT_DEADLINE_UPCOMING, "Upcoming Deadline"),
        (EVENT_DEADLINE_NEARING, "Nearing Deadline"),
        (EVENT_MISSED, "Missed Assessment"),
    ]

    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE, related_name="notifications")
    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.CASCADE, related_name="assessment_notifications"
    )
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)

    # JSON payload for event-specific details, e.g.
    # {"hours_before_deadline": 24} or {"deadline": "2026-09-10T23:59:00Z"}
    metadata = models.JSONField(default=dict, blank=True)

    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["student", "is_read"]),
            models.Index(fields=["assessment", "event_type"]),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.student} - {self.assessment}"


# ---------------------------------------------------------------------------
# Assessment integrity / proctoring
# ---------------------------------------------------------------------------
class AssessmentProctoringEvent(models.Model):
    """A focus, visibility, or screenshot-signal event during an assessment.

    The browser can only *notice* these signals and report them; it cannot
    truly prevent a screenshot or a second monitor. So this model stores what
    was detected rather than claiming a block: a student who leaves the paper
    generates a row the teacher can review, exactly as the manuscript's
    "record detected events" rule asks. ``event_type`` stays a loose choice
    list so the client's vocabulary and the database cannot drift apart.
    """

    EVENT_FOCUS_LOST = "focus_lost"
    EVENT_FOCUS_GAINED = "focus_gained"
    EVENT_VISIBILITY_HIDDEN = "visibility_hidden"
    EVENT_VISIBILITY_VISIBLE = "visibility_visible"
    EVENT_SCREENSHOT_KEY = "screenshot_key"
    EVENT_COPY_ATTEMPT = "copy_attempt"
    EVENT_CONTEXT_MENU = "context_menu"
    EVENT_TYPE_CHOICES = [
        (EVENT_FOCUS_LOST, "Left the assessment window"),
        (EVENT_FOCUS_GAINED, "Returned to the assessment window"),
        (EVENT_VISIBILITY_HIDDEN, "Switched away from the assessment tab"),
        (EVENT_VISIBILITY_VISIBLE, "Returned to the assessment tab"),
        (EVENT_SCREENSHOT_KEY, "Screenshot-type key pressed"),
        (EVENT_COPY_ATTEMPT, "Copy / cut attempt"),
        (EVENT_CONTEXT_MENU, "Right-click on question content"),
    ]

    assessment = models.ForeignKey(
        Assessment, on_delete=models.CASCADE, related_name="proctoring_events"
    )
    student = models.ForeignKey(
        "accounts.StudentProfile", on_delete=models.CASCADE, related_name="proctoring_events"
    )
    event_type = models.CharField(max_length=30, choices=EVENT_TYPE_CHOICES)
    # Free-form context, e.g. {"source": "visibilitychange", "hidden_seconds": 12}.
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["assessment", "student"]),
            models.Index(fields=["assessment", "event_type"]),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.student} @ {self.assessment}"
