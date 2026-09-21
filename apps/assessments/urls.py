from django.urls import path

from . import views

app_name = "assessments"

urlpatterns = [
    path("list/", views.assessment_list, name="assessment_list"),
    path("add/", views.assessment_add, name="assessment_add"),
    path("<int:pk>/", views.assessment_detail, name="assessment_detail"),
    path("<int:pk>/update/", views.assessment_update, name="assessment_update"),
    path("<int:pk>/archive/", views.assessment_archive, name="assessment_archive"),
    path("<int:pk>/assign/", views.assessment_assign, name="assessment_assign"),
    path("<int:pk>/submit/", views.assessment_submit, name="assessment_submit"),
    path("<int:pk>/parts/add/", views.assessment_part_add, name="assessment_part_add"),
    path(
        "<int:pk>/parts/<int:part_pk>/update/",
        views.assessment_part_update,
        name="assessment_part_update",
    ),
    path(
        "<int:pk>/parts/<int:part_pk>/delete/",
        views.assessment_part_delete,
        name="assessment_part_delete",
    ),
    path(
        "<int:pk>/submissions/",
        views.submissions_monitor,
        name="submissions_monitor",
    ),
    path(
        "<int:pk>/notifications/trigger/",
        views.trigger_notifications,
        name="trigger_notifications",
    ),
    path(
        "<int:pk>/submissions/<int:submission_pk>/grade/",
        views.grade_submission,
        name="grade_submission",
    ),
    path(
        "formula/<int:vc_pk>/",
        views.grading_formula,
        name="grading_formula",
    ),
    path(
        "gradebook/<int:vc_pk>/",
        views.grade_book,
        name="grade_book",
    ),
    path(
        "performance/",
        views.performance_tracking,
        name="performance_tracking",
    ),
    path("repository/", views.assessment_repository, name="assessment_repository"),
]