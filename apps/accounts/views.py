import json
import uuid

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login as django_login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from apps.academic_structure.models import AcademicYear
from apps.system_settings.models import PasswordPolicy, SystemConfiguration
from .models import (
    ActivityLog,
    EmailVerificationToken,
    LoginSession,
    PasswordHistory,
    PasswordResetToken,
    RoleAssignment,
    User,
)

DEFAULT_MAX_FAILED_ATTEMPTS = 5
RESET_TOKEN_EXPIRY_MINUTES = 30
VERIFY_TOKEN_EXPIRY_HOURS = 48

ROLE_DASHBOARD_URLS = {
    "system_admin": "dashboards:ict",
    "registrar": "dashboards:registrar",
    "teacher": "dashboards:teacher",
    "principal": "dashboards:principal",
    "assistant_principal": "dashboards:academic_coordinator",
    "student": "dashboards:student",
    "tass_administrator": "dashboards:tass_administrator",
    "quality_audit": "dashboards:quality_audit",
}


def _max_failed_attempts():
    """Lockout threshold from System Settings (configurable), with a safe default."""
    try:
        value = SystemConfiguration.get_solo().account_lockout_threshold
        return value or DEFAULT_MAX_FAILED_ATTEMPTS
    except Exception:
        return DEFAULT_MAX_FAILED_ATTEMPTS


def login_page(request):
    session_expired = request.session.pop("session_expired", False)
    return render(
        request,
        "accounts/login.html",
        {"session_expired": session_expired},
    )


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
        max_attempts = _max_failed_attempts()
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= max_attempts:
            user.status = User.Status.LOCKED
            user.save()
            ActivityLog.objects.create(
                user=user,
                action="account_locked",
                related_entity_type="User",
                related_entity_id=user.pk,
            )
            return JsonResponse({
                "ok": False,
                "reason": "locked",
                "message": "Too many failed attempts. This account is now locked. Contact your System Administrator or reset your password.",
            }, status=403)

        user.save()
        remaining = max_attempts - user.failed_login_attempts
        return JsonResponse({
            "ok": False,
            "message": f"The email or password you entered is wrong. {remaining} attempt(s) remaining before this account is locked.",
        }, status=401)

    authenticated_user.failed_login_attempts = 0
    if authenticated_user.password_changed_at is None:
        authenticated_user.password_changed_at = timezone.now()
    authenticated_user.save(update_fields=["failed_login_attempts", "password_changed_at"])

    role_assignments = list(
        RoleAssignment.objects
        .filter(user=authenticated_user, academic_year__status=AcademicYear.STATUS_ACTIVE)
        .select_related("role")
    )

    if not role_assignments:
        return JsonResponse({
            "ok": False,
            "message": "This account has no active role assignment for the current academic year. Contact your Registrar or System Administrator.",
        }, status=403)

    # Deterministic role selection when a user holds multiple roles:
    # pick the first match against ROLE_ORDER (system_admin > registrar > …).
    from apps.accounts.context_processors import ROLE_ORDER

    def _role_priority(ra):
        try:
            return ROLE_ORDER.index(ra.role.code)
        except ValueError:
            return len(ROLE_ORDER)

    role_assignment = min(role_assignments, key=_role_priority)

    django_login(request, authenticated_user)

    device_info = request.META.get("HTTP_USER_AGENT", "")[:255]
    login_session = LoginSession.objects.create(
        user=authenticated_user,
        device_info=device_info,
        ip_address=request.META.get("REMOTE_ADDR"),
        session_key=request.session.session_key,
    )

    if device_info:
        seen_before = LoginSession.objects.filter(
            user=authenticated_user,
            device_info=device_info,
        ).exclude(pk=login_session.pk).exists()
        if not seen_before:
            send_mail(
                subject="TASS LMS – New Device Sign-In",
                message=(
                    f"Hello {authenticated_user.get_full_name()},\n\n"
                    f"Your account was just signed into from a new device.\n"
                    f"Device: {device_info}\n"
                    f"IP address: {request.META.get('REMOTE_ADDR')}\n"
                    f"Time: {timezone.now():%Y-%m-%d %H:%M:%S %Z}\n\n"
                    f"If this was you, you can ignore this message. If not, "
                    f"reset your password and review your active sessions."
                ),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@tass-lms.local"),
                recipient_list=[authenticated_user.email],
                fail_silently=True,
            )
            ActivityLog.objects.create(
                user=authenticated_user,
                action="new_device_login",
                related_entity_type="LoginSession",
                related_entity_id=login_session.pk,
            )

    ActivityLog.objects.create(
        user=authenticated_user,
        action="user_login",
        related_entity_type="User",
        related_entity_id=authenticated_user.pk,
    )

    redirect_url = reverse(ROLE_DASHBOARD_URLS[role_assignment.role.code])
    policy = PasswordPolicy.get_solo()
    if policy.expiration_days and authenticated_user.password_changed_at:
        if (timezone.now() - authenticated_user.password_changed_at).days >= policy.expiration_days:
            request.session["force_password_change"] = True
            redirect_url = reverse("accounts:change_password") + "?expired=1"
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
        ActivityLog.objects.create(
            user=user,
            action="forgot_password_requested",
            related_entity_type="User",
            related_entity_id=user.pk,
        )

    return JsonResponse({"ok": True})


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

        if new_password != confirm_password:
            return JsonResponse({"ok": False, "message": "Passwords must match."}, status=400)

        violations = _password_policy_violations(new_password)
        if violations:
            return JsonResponse(
                {"ok": False, "message": "Password must include " + ", ".join(violations) + "."},
                status=400,
            )

        user = record.user
        if _password_reused(user, new_password):
            return JsonResponse(
                {"ok": False, "message": "You have used this password recently. Choose a different password."},
                status=400,
            )

        user.password = make_password(new_password)
        user.failed_login_attempts = 0
        was_locked = user.status in (User.Status.LOCKED, User.Status.PENDING_PASSWORD_SETUP)
        if hasattr(user, "password_changed_at"):
            user.password_changed_at = timezone.now()
        if was_locked:
            user.status = User.Status.ACTIVE
        update_fields = ["password", "failed_login_attempts"] + (["password_changed_at"] if hasattr(user, "password_changed_at") else [])
        if was_locked:
            update_fields.append("status")
        user.save(update_fields=update_fields)
        _record_password_history(user)
        record.used_at = timezone.now()
        record.save(update_fields=["used_at"])
        return JsonResponse({"ok": True, "redirect_url": reverse("accounts:login_page")})

    return render(request, "accounts/password_reset_confirm.html", {"token": token, "invalid": False})


# ---------------------------------------------------------------------------
# Password policy & history
# ---------------------------------------------------------------------------
def _password_policy_violations(value):
    """Return a list of rules the given plaintext password violates."""
    policy = PasswordPolicy.get_solo()
    problems = []
    if len(value) < policy.minimum_length:
        problems.append(f"at least {policy.minimum_length} characters")
    if policy.require_uppercase and not any(c.isupper() for c in value):
        problems.append("an uppercase letter")
    if policy.require_lowercase and not any(c.islower() for c in value):
        problems.append("a lowercase letter")
    if policy.require_number and not any(c.isdigit() for c in value):
        problems.append("a number")
    if policy.require_symbol and not any(not c.isalnum() for c in value):
        problems.append("a symbol")
    return problems


def _password_reused(user, value):
    """True if the plaintext value matches any recent password in history."""
    limit = max(PasswordPolicy.get_solo().reuse_limit or 0, 1)
    history = PasswordHistory.objects.filter(user=user).order_by("-created_at")[:limit]
    return any(
        check_password(value, record.hashed_password) for record in history
    )


def _record_password_history(user):
    """Store the user's current (newly set) password and prune to reuse_limit."""
    PasswordHistory.objects.create(user=user, hashed_password=user.password)
    limit = PasswordPolicy.get_solo().reuse_limit
    if not limit:
        return
    history = PasswordHistory.objects.filter(user=user).order_by("-created_at")
    excess = history.count() - max(int(limit), 1)
    if excess > 0:
        history_list = list(history)
        for record in history_list[int(limit):]:
            record.delete()


@login_required
def change_password(request):
    from .forms import ChangePasswordForm

    expired = bool(request.GET.get("expired") or request.session.pop("force_password_change", False))
    form = ChangePasswordForm(request.POST or None)
    if request.method == "POST":
        form = ChangePasswordForm(request.POST)
        if form.is_valid():
            current = form.cleaned_data["current_password"]
            new = form.cleaned_data["new_password"]
            if not check_password(current, request.user.password):
                form.add_error("current_password", "Your current password is incorrect.")
            else:
                violations = _password_policy_violations(new)
                if violations:
                    form.add_error(
                        "new_password",
                        "Password must include " + ", ".join(violations) + ".",
                    )
                elif _password_reused(request.user, new):
                    form.add_error(
                        "new_password",
                        "You have used this password recently. Choose a different password.",
                    )
                else:
                    request.user.password = make_password(new)
                    request.user.password_changed_at = timezone.now()
                    request.user.save(update_fields=["password", "password_changed_at"])
                    _record_password_history(request.user)
                    ActivityLog.objects.create(
                        user=request.user,
                        action="password_changed",
                        related_entity_type="User",
                        related_entity_id=request.user.pk,
                    )
                    update_session_auth_hash(request, request.user)
                    from django.contrib import messages
                    messages.success(request, "Your password has been updated successfully.")
                    return redirect("accounts:my_sessions")
    context = {"form": form, "expired": expired}
    return render(request, "accounts/change_password.html", context)


@login_required
def my_sessions(request):
    sessions = request.user.sessions.order_by("-last_active_at")[:20]
    context = {"sessions": sessions}
    return render(request, "accounts/my_sessions.html", context)


@login_required
def revoke_session(request, pk):
    from django.contrib.sessions.models import Session

    session = get_object_or_404(LoginSession, pk=pk, user=request.user)
    if session.session_key:
        Session.objects.filter(session_key=session.session_key).delete()
    session.delete()
    ActivityLog.objects.create(
        user=request.user,
        action="session_revoked",
        related_entity_type="LoginSession",
        related_entity_id=pk,
    )
    from django.contrib import messages
    messages.success(request, "Session revoked.")
    return redirect("accounts:my_sessions")


@require_http_methods(["POST"])
@login_required
def logout_other_sessions(request):
    from django.contrib.sessions.models import Session

    current_key = request.session.session_key
    other = request.user.sessions.exclude(session_key=current_key)
    keys = [s.session_key for s in other if s.session_key]
    Session.objects.filter(session_key__in=keys).delete()
    count = other.count()
    other.delete()
    ActivityLog.objects.create(
        user=request.user,
        action="logout_other_sessions",
        related_entity_type="User",
        related_entity_id=request.user.pk,
    )
    from django.contrib import messages
    messages.success(request, f"Signed out {count} other session(s).")
    return redirect("accounts:my_sessions")


def _send_verification_email(user, request, password="", recipient_name=""):
    token_value = str(uuid.uuid4())
    expires_at = timezone.now() + timedelta(hours=VERIFY_TOKEN_EXPIRY_HOURS)
    EmailVerificationToken.objects.create(
        user=user, token=token_value, expires_at=expires_at
    )
    verify_url = request.build_absolute_uri(
        reverse("accounts:verify_email", args=[token_value])
    )

    name = recipient_name or user.get_full_name() or user.email
    kind = "employee"
    try:
        if hasattr(user, "student_profile") and user.student_profile_id:
            kind = "student"
    except Exception:
        pass

    context = {
        "greeting_name": name,
        "login_email": user.email,
        "login_password": password,
        "verify_url": verify_url,
        "expiry_hours": VERIFY_TOKEN_EXPIRY_HOURS,
        "school_name": getattr(settings, "SCHOOL_NAME", "Saint Joseph's College of Baggao"),
        "contact_email": getattr(settings, "SCHOOL_CONTACT_EMAIL", "info@whitestrings.io"),
        "account_kind": kind,
    }
    from .brevo import send_html_email, verification_email_html, verification_email_text
    try:
        send_html_email(
            subject="Verify your email address",
            to_email=user.email,
            to_name=name,
            html=verification_email_html(context),
            text=verification_email_text(context),
        )
    except Exception:
        import logging
        logging.getLogger("accounts").exception(
            "Failed to send verification email to %s", user.email
        )
    return token_value


def verify_email(request, token):
    record = (
        EmailVerificationToken.objects
        .filter(token=token, verified_at__isnull=True)
        .select_related("user")
        .first()
    )
    context = {"ok": False}
    if record is not None and record.expires_at >= timezone.now():
        record.verified_at = timezone.now()
        record.save(update_fields=["verified_at"])
        user = record.user
        if user.status == User.Status.PENDING_VERIFICATION:
            user.status = User.Status.ACTIVE
            user.save(update_fields=["status"])
        ActivityLog.objects.create(
            user=user,
            action="email_verified",
            related_entity_type="User",
            related_entity_id=user.pk,
        )
        context = {"ok": True, "email": user.email}
    return render(request, "accounts/verify_email.html", context)