import json
import uuid

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login as django_login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from django.core.mail import send_mail
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from apps.academic_structure.models import AcademicYear
from .models import LoginSession, PasswordResetToken, RoleAssignment, User

MAX_FAILED_ATTEMPTS = 5
RESET_TOKEN_EXPIRY_MINUTES = 30

ROLE_DASHBOARD_URLS = {
    "system_admin": "dashboards:ict",
    "registrar": "dashboards:registrar",
    "teacher": "dashboards:teacher",
    "principal": "dashboards:principal",
    "assistant_principal": "dashboards:academic_coordinator",
    "student": "dashboards:student",
}


def login_page(request):
    return render(request, "accounts/login.html")


@require_http_methods(["POST", "GET"])
def logout_view(request):
    if request.method == "POST":
        logout(request)
        return redirect("accounts:login_page")
    return redirect("accounts:login_page")


@csrf_protect
@require_http_methods(["POST"])
def login_view(request):
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"ok": False, "message": "Malformed request."}, status=400)

    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    if not email or not password:
        return JsonResponse({"ok": False, "message": "Email and password are required."}, status=400)

    user = User.objects.filter(email=email).first()

    if user is None:
        return JsonResponse({"ok": False, "message": "The email or password you entered is wrong."}, status=401)

    if user.status == User.Status.LOCKED:
        return JsonResponse({
            "ok": False,
            "reason": "locked",
            "message": "Too many failed attempts. Contact your System Administrator to unlock this account, or reset your password.",
        }, status=403)

    if user.status == User.Status.ARCHIVED:
        return JsonResponse({
            "ok": False,
            "reason": "archived",
            "message": "This account has been archived and can no longer sign in.",
        }, status=403)

    if user.status == User.Status.PENDING_VERIFICATION:
        return JsonResponse({
            "ok": False,
            "reason": "pending_verification",
            "message": "Please verify your email address before signing in.",
        }, status=403)

    if user.status == User.Status.PENDING_PASSWORD_SETUP:
        return JsonResponse({
            "ok": False,
            "reason": "pending_password_setup",
            "message": "Please finish setting up your password before signing in.",
        }, status=403)

    authenticated_user = authenticate(request, username=email, password=password)

    if authenticated_user is None:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
            user.status = User.Status.LOCKED
            user.save()
            return JsonResponse({
                "ok": False,
                "reason": "locked",
                "message": "Too many failed attempts. This account is now locked. Contact your System Administrator or reset your password.",
            }, status=403)

        user.save()
        remaining = MAX_FAILED_ATTEMPTS - user.failed_login_attempts
        return JsonResponse({
            "ok": False,
            "message": f"The email or password you entered is wrong. {remaining} attempt(s) remaining before this account is locked.",
        }, status=401)

    authenticated_user.failed_login_attempts = 0
    authenticated_user.save(update_fields=["failed_login_attempts"])

    django_login(request, authenticated_user)

    LoginSession.objects.create(
        user=authenticated_user,
        device_info=request.META.get("HTTP_USER_AGENT", "")[:255],
        ip_address=request.META.get("REMOTE_ADDR"),
    )

    role_assignment = (
        RoleAssignment.objects
        .filter(user=authenticated_user, academic_year__status=AcademicYear.STATUS_ACTIVE)
        .select_related("role")
        .first()
    )

    if role_assignment is None:
        return JsonResponse({
            "ok": False,
            "message": "This account has no active role assignment for the current academic year. Contact your Registrar or System Administrator.",
        }, status=403)

    redirect_url = reverse(ROLE_DASHBOARD_URLS[role_assignment.role.code])
    return JsonResponse({"ok": True, "redirect_url": redirect_url})


@csrf_protect
@require_http_methods(["POST"])
def forgot_password(request):
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"ok": False, "message": "Malformed request."}, status=400)

    email = (payload.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"ok": False, "message": "A valid email address is required."}, status=400)

    user = User.objects.filter(email=email).first()
    if user:
        token_value = str(uuid.uuid4())
        expires_at = timezone.now() + timedelta(minutes=RESET_TOKEN_EXPIRY_MINUTES)
        PasswordResetToken.objects.create(user=user, token=token_value, expires_at=expires_at)
        reset_url = request.build_absolute_uri(
            reverse("accounts:reset_password_confirm", args=[token_value])
        )
        send_mail(
            subject="TASS LMS – Reset Your Password",
            message=(
                f"Hello,\n\n"
                f"You requested a password reset. Click the link below to set a new password.\n\n"
                f"{reset_url}\n\n"
                f"This link will expire in {RESET_TOKEN_EXPIRY_MINUTES} minutes.\n\n"
                f"If you did not request this, please ignore this email."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@tass-lms.local"),
            recipient_list=[user.email],
            fail_silently=True,
        )

    return JsonResponse({"ok": True})


def _valid_password(value):
    return isinstance(value, str) and len(value) >= 8


def reset_password_confirm(request, token):
    record = PasswordResetToken.objects.filter(token=token, used_at__isnull=True).select_related("user").first()
    if record is None or record.expires_at < timezone.now():
        return render(request, "accounts/password_reset_confirm.html", {"invalid": True})

    if request.method == "POST":
        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, TypeError):
            payload = {}

        new_password = payload.get("new_password") or request.POST.get("new_password", "")
        confirm_password = payload.get("confirm_password") or request.POST.get("confirm_password", "")

        if not _valid_password(new_password) or new_password != confirm_password:
            return JsonResponse({"ok": False, "message": "Password must be at least 8 characters and passwords must match."}, status=400)

        user = record.user
        user.password = make_password(new_password)
        user.failed_login_attempts = 0
        if hasattr(user, "password_changed_at"):
            user.password_changed_at = timezone.now()
        user.save(update_fields=["password", "failed_login_attempts"] + (["password_changed_at"] if hasattr(user, "password_changed_at") else []))
        record.used_at = timezone.now()
        record.save(update_fields=["used_at"])
        return JsonResponse({"ok": True, "redirect_url": reverse("accounts:login_page")})

    return render(request, "accounts/password_reset_confirm.html", {"token": token, "invalid": False})