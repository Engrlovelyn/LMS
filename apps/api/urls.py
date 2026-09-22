from django.urls import path

from . import views

urlpatterns = [
    path("csrf/", views.csrf, name="api_csrf"),
    path("accounts/", views.accounts, name="api_accounts"),
    path("students/", views.students, name="api_students"),
    path("academic-structure/", views.academic_structure, name="api_academic_structure"),
    path("school-announcements/", views.school_announcements, name="api_school_announcements"),
    path("class-announcements/", views.class_announcements, name="api_class_announcements"),
    path("profile/", views.profile, name="api_profile"),
    path("notifications/read/", views.notifications_read, name="api_notifications_read"),
    path("teacher-content/", views.teacher_content, name="api_teacher_content"),
    path("attendance/", views.attendance, name="api_attendance"),
    path("assessments/", views.assessments, name="api_assessments"),
    path("system-settings/", views.system_settings, name="api_system_settings"),
    path("password-policy/", views.password_policy, name="api_password_policy"),
    path("backup/", views.backup, name="api_backup"),
    path("role-access-review/", views.role_access_review, name="api_role_access_review"),
    path("messages/", views.messages, name="api_messages"),
]