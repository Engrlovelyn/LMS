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
    path("<int:pk>/submissions/", views.submissions_monitor, name="submissions_monitor"),
    path(
        "<int:pk>/submissions/<int:submission_pk>/grade/",
        views.grade_submission,
        name="grade_submission",
    ),
    path("repository/", views.assessment_repository, name="assessment_repository"),
    path("list/", views.assessment_list, name="list"),
]
