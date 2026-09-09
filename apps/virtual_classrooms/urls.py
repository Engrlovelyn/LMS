from django.urls import path

from . import views

app_name = "classrooms"

urlpatterns = [
    path("list/", views.classroom_list, name="classroom_list"),
    path("add/", views.classroom_add, name="classroom_add"),
    path("<int:pk>/", views.classroom_detail, name="classroom_detail"),
    path("<int:pk>/update/", views.classroom_update, name="classroom_update"),
    path("<int:pk>/archive/", views.classroom_archive, name="classroom_archive"),
    path("<int:pk>/enroll/", views.classroom_enroll, name="classroom_enroll"),
    path("<int:pk>/enroll-student/", views.classroom_add_student, name="classroom_add_student"),
    path(
        "<int:pk>/students/<int:enrollment_pk>/remove/",
        views.classroom_remove_student,
        name="classroom_remove_student",
    ),
]
