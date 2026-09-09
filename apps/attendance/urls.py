from django.urls import path

from . import views

app_name = "attendance"

urlpatterns = [
    path("index/", views.attendance_index, name="attendance_index"),
    path("classes/<int:vc_pk>/", views.class_sessions, name="class_sessions"),
    path("classes/<int:vc_pk>/record/", views.record_attendance, name="record_attendance"),
    path("sessions/<int:pk>/", views.session_detail, name="session_detail"),
    path("sessions/<int:pk>/update/", views.session_update, name="session_update"),
    path(
        "sessions/<int:pk>/change-status/",
        views.record_status_update,
        name="record_status_update",
    ),
    path("report/", views.attendance_report, name="attendance_report"),
]
