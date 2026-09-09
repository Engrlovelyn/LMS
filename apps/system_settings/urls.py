from django.urls import path

from . import views

app_name = "settings"

urlpatterns = [
    path("index/", views.settings_index, name="index"),
    path("password-policy/", views.password_policy, name="password_policy"),
    path("attendance-threshold/", views.attendance_threshold, name="attendance_threshold"),
    path("backups/", views.backup_list, name="backups"),
    path("backups/create/", views.backup_create, name="backup_create"),
    path("backups/<int:pk>/restore/", views.backup_restore, name="backup_restore"),
    path("backups/<int:pk>/download/", views.backup_download, name="backup_download"),
]
