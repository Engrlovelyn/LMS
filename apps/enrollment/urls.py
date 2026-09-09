from django.urls import path

from . import views

app_name = "enrollment"

urlpatterns = [
    path("", views.enrollment_dashboard, name="dashboard"),
    path("classrooms/<int:pk>/", views.classroom_enrollment_detail, name="classroom_enrollment_detail"),
    path("classrooms/<int:pk>/enroll/", views.enroll_students, name="enroll_students"),
    path(
        "classrooms/<int:pk>/students/<int:enrollment_pk>/remove/",
        views.remove_student,
        name="remove_student",
    ),
]