import json
import threading
import uuid

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, login as django_login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password, make_password
from django.core.files.storage import default_storage
from django.core.mail import send_mail
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods

from apps.academic_structure.models import AcademicYear
from apps.system_settings.models import PasswordPolicy, SystemConfiguration
from .account_admin import lock_user_account
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
    "elementary_principal": "dashboards:principal",
    "assistant_principal": "dashboards:academic_coordinator",
    "student": "dashboards:student",
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


@csrf_exempt
@require_http_methods(["POST", "GET"])
def logout_view(request):
    logout(request)
    return redirect("accounts:login_page")


@csrf_protect
@require_http_methods(["POST"])
def _send_new_device_notice(user, device_info, ip_address):
    """Send the courtesy "new device" email off the request path.

    Runs on a daemon thread from the login view, so a slow SMTP relay can never
    hold up the sign-in. ``fail_silently`` hides delivery problems from the
    login outcome, and ``EMAIL_TIMEOUT`` (settings) caps how long a relay may
    take before the thread gives up.
    """
    try:
        send_mail(
            subject="SVI LMS – New Device Sign-In",
            message=(
                "Hello {},\n\n"
                "Your account was just signed into from a new device.\n"
                "Device: {}\n"
                "IP address: {}\n"
                "Time: {:%Y-%m-%d %H:%M:%S %Z}\n\n"
                "If this was you, you can ignore this message. If not, "
                "reset your password and review your active sessions."
            ).format(
                user.get_full_name(),
                device_info,
                ip_address,
                timezone.now(),
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@svi-lms.local"),
            recipient_list=[user.email],
            fail_silently=True,
        )
    except Exception:
        # A courtesy notice must never break the sign-in its thread is racing.
        pass


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
            lock_user_account(user)
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

    # Handle remember me
    remember_me = payload.get("remember_me", False) if 'payload' in locals() else False
    if remember_me:
        request.session.set_expiry(60 * 60 * 24 * 30)  # 30 days
    else:
        request.session.set_expiry(0)  # browser session expires

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
            # The new-device notice is a courtesy email: it must never make the
            # sign-in wait on SMTP. It used to be sent in the request, so a
            # slow or offline relay stalled the login POST for its full socket
            # timeout. Fire a daemon thread instead and return straight away.
            threading.Thread(
                target=lambda: _send_new_device_notice(
                    authenticated_user, device_info,
                    request.META.get("REMOTE_ADDR"),
                ),
                daemon=True,
            ).start()
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

    # An administrator issued this password on the user's behalf, so the user
    # knows it and must not keep it. This outranks the expiry check below: a
    # freshly reset password has a brand-new password_changed_at and would
    # otherwise sail past the expiry rule.
    if authenticated_user.must_change_password:
        request.session["force_password_change"] = True
        redirect_url = reverse("accounts:change_password") + "?reset=1"
    elif policy.expiration_days and authenticated_user.password_changed_at:
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
            subject="SVI LMS – Reset Your Password",
            message=(
                f"Hello,\n\n"
                f"You requested a password reset. Click the link below to set a new password.\n\n"
                f"{reset_url}\n\n"
                f"This link will expire in {RESET_TOKEN_EXPIRY_MINUTES} minutes.\n\n"
                f"If you did not request this, please ignore this email."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@svi-lms.local"),
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
        user.locked_at = None
        # Setting their own password through the emailed link satisfies an
        # outstanding admin-issued temporary password.
        user.must_change_password = False
        was_locked = user.status in (User.Status.LOCKED, User.Status.PENDING_PASSWORD_SETUP)
        if hasattr(user, "password_changed_at"):
            user.password_changed_at = timezone.now()
        if was_locked:
            user.status = User.Status.ACTIVE
        update_fields = [
            "password",
            "failed_login_attempts",
            "locked_at",
            "must_change_password",
        ]
        if hasattr(user, "password_changed_at"):
            update_fields.append("password_changed_at")
        if was_locked:
            update_fields.extend(["status", "is_active"])
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
    # Distinguishes "your password aged out" from "an administrator set a
    # password for you", which is a different explanation for the user.
    admin_reset = bool(request.GET.get("reset")) or bool(getattr(request.user, "must_change_password", False))
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
                    request.user.must_change_password = False
                    request.user.save(
                        update_fields=["password", "password_changed_at", "must_change_password"]
                    )
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
    context = {"form": form, "expired": expired, "admin_reset": admin_reset}
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
    name = recipient_name or user.get_full_name() or user.email
    kind = "employee"
    if getattr(user, "student_profile", None) is not None:
        kind = "student"

    from .email_branding import (
        send_html_email,
        verification_email_html,
        verification_email_text,
    )
    try:
        verify_url = request.build_absolute_uri(
            reverse("accounts:verify_email", args=[token_value])
        )
        context = {
            "greeting_name": name,
            "login_email": user.email,
            "login_password": password,
            "verify_url": verify_url,
            "expiry_hours": VERIFY_TOKEN_EXPIRY_HOURS,
            "school_name": getattr(settings, "SCHOOL_NAME", "San Vicente Institute"),
            "contact_email": getattr(settings, "SCHOOL_CONTACT_EMAIL", "info@whitestrings.io"),
            "account_kind": kind,
        }
        html = verification_email_html(context)
        text = verification_email_text(context)
        send_html_email(
            subject="Verify your email address",
            to_email=user.email,
            to_name=name,
            html=html,
            text=text,
            logo_cid="SVI_logo.png",
        )
    except Exception as exc:
        import logging
        logging.getLogger("accounts").exception(
            "Failed to send verification email to %s", user.email
        )
        return {
            "ok": False,
            "user": user,
            "message": f"The verification email could not be sent to {user.email}: {exc}",
        }
    return {"ok": True, "user": user, "token": token_value}


def verify_email(request, token):
    record = (
        EmailVerificationToken.objects
        .filter(token=token, verified_at__isnull=True)
        .select_related("user")
        .first()
    )
    context = {"ok": False, "expired": False}
    if record is not None and record.expires_at >= timezone.now():
        record.verified_at = timezone.now()
        record.save(update_fields=["verified_at"])
        user = record.user
        if user.status == User.Status.PENDING_VERIFICATION:
            user.status = User.Status.ACTIVE
            user.save(update_fields=["status", "is_active"])
        ActivityLog.objects.create(
            user=user,
            action="email_verified",
            related_entity_type="User",
            related_entity_id=user.pk,
        )
        # Show a short "Email successfully verified" confirmation, then send
        # the user straight to the login page.
        context = {
            "ok": True,
            "email": user.email,
            "next_url": reverse("accounts:login_page"),
            "user_email": user.email,
        }
    elif record is not None:
        # Link older than 48 hours: expired, not yet verified.
        context = {"ok": False, "expired": True}
    return render(request, "accounts/verify_email.html", context)

# ---------------------------------------------------------------------------
# Avatar delivery
# ---------------------------------------------------------------------------
# Avatar files live in the default storage, which is private Cloudflare R2 in
# production (settings.AWS_QUERYSTRING_AUTH = True). `FieldFile.url` therefore
# returns a *presigned* URL carrying `X-Amz-Expires` (one hour by default).
#
# Rendering that URL straight into a page is what made profile photos vanish:
# the HTML was correct at render time, but once the page had been open (or sat
# in a back/forward cache) longer than the signature lifetime, R2 rejected the
# image and the browser dropped it. Every dashboard was affected because they
# all read `user_avatar_url`.
#
# The fix is to keep a stable, same-origin path in the HTML and mint the
# signature per request instead.
AVATAR_PREFIX = "avatars/"


def _avatar_storage_name(name):
    """Return `name` if it is a safe avatars/ object key, else None."""
    if not name or len(name) > 255:
        return None
    if name.startswith("/") or "\\" in name or ".." in name:
        return None
    if not name.startswith(AVATAR_PREFIX):
        return None
    return name


@require_http_methods(["GET", "HEAD"])
@login_required
def avatar_redirect(request, name):
    """302 to a freshly signed URL for the stored avatar `name`.

    Auth is required because the avatars are private objects; the path is
    scoped to the `avatars/` prefix so it cannot be used to fetch arbitrary
    keys out of the bucket.
    """
    storage_name = _avatar_storage_name(name)
    if storage_name is None:
        raise Http404("Not found.")
    try:
        target = default_storage.url(storage_name)
    except (NotImplementedError, ValueError):
        raise Http404("Not found.")
    response = HttpResponseRedirect(target)
    # The redirect target is signed per request and must not be cached, or a
    # stale signature would outlive its own expiry.
    response["Cache-Control"] = "private, no-store"
    return response
