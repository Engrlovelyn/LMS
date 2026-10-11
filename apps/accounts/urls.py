from django.urls import path

from . import management_views, views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_page, name="login_page"),
    path("api/login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("api/forgot/", views.forgot_password, name="forgot_password"),
    path("reset/<slug:token>/", views.reset_password_confirm, name="reset_password_confirm"),
    path("verify/<uuid:token>/", views.verify_email, name="verify_email"),
    path("change-password/", views.change_password, name="change_password"),
    path("sessions/", views.my_sessions, name="my_sessions"),
    path("sessions/<int:pk>/revoke/", views.revoke_session, name="revoke_session"),
    path("sessions/logout-others/", views.logout_other_sessions, name="logout_other_sessions"),
    path("avatar/<path:name>/", views.avatar_redirect, name="avatar"),
    path("employees/", management_views.employee_account_list, name="employee_account_list"),
    path("employees/add/", management_views.employee_account_add, name="employee_account_add"),
    path("employees/<int:pk>/update/", management_views.employee_account_update, name="employee_account_update"),
    path("employees/<int:pk>/archive/", management_views.employee_account_archive, name="employee_account_archive"),
    path("employees/<int:pk>/reset-password/", management_views.admin_reset_password, name="admin_reset_password"),
    # Account recovery is keyed on User pk rather than on the employee list so
    # it works for a student account too -- a student who cannot receive the
    # emailed reset link needs the same fallback an employee does.
    path("reset-password/generate/", management_views.admin_reset_password_generate, name="admin_reset_password_generate"),
    path("accounts/<int:pk>/unlock/", management_views.account_unlock, name="account_unlock"),
    path("students/", management_views.student_record_list, name="student_record_list"),
    path("students/add/", management_views.student_record_add, name="student_record_add"),
    path("students/generate/", management_views.student_record_generate, name="student_record_generate"),
    path("students/<int:pk>/update/", management_views.student_record_update, name="student_record_update"),
    path("students/<int:pk>/archive/", management_views.student_record_archive, name="student_record_archive"),
    path("students/import/", management_views.student_records_import, name="student_records_import"),
    path("classes/<int:vc_pk>/import-students/", management_views.vc_students_import, name="vc_students_import"),
    path("role-assignments/", management_views.role_assignment_list, name="role_assignment_list"),
    path("role-assignments/add/", management_views.role_assignment_add, name="role_assignment_add"),
    path("role-assignments/<int:pk>/remove/", management_views.role_assignment_remove, name="role_assignment_remove"),
]