from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("index/", views.analytics_index, name="analytics_index"),
    path("school/", views.school_analytics, name="school_analytics"),
    path("class/<int:vc_pk>/", views.class_analytics, name="class_analytics"),
    path("attendance/", views.attendance_trends, name="attendance_trends"),
    path("reports/", views.submission_reports, name="submission_reports"),
    path("sf/", views.sf_report_home, name="sf_report_home"),
    path("reports/<int:pk>/download/", views.report_download, name="report_download"),
    path("activity-logs/", views.activity_logs, name="activity_logs"),
]
