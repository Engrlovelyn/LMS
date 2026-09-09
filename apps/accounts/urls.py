from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_page, name="login_page"),
    path("api/login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("api/forgot/", views.forgot_password, name="forgot_password"),
    path("reset/<slug:token>/", views.reset_password_confirm, name="reset_password_confirm"),
]