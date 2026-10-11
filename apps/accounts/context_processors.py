from django.urls import NoReverseMatch, reverse

# Tie-breaker for a user who holds several roles. Order matters: the first
# match here wins, so a teacher who is also a principal lands on the principal
# dashboard. Keep both principal codes adjacent and High School first, matching
# the campus order used by `apps.accounts.campus`.
ROLE_ORDER = [
    "system_admin",
    "registrar",
    "principal",
    "elementary_principal",
    "assistant_principal",
    "teacher",
    "student",
    "quality_audit",
]

ROLE_LABELS = {
    "system_admin": "System Administrator",
    "registrar": "Registrar",
    "principal": "High School Principal",
    "elementary_principal": "Elementary Principal",
    "assistant_principal": "Assistant Principal",
    "teacher": "Teacher",
    "student": "Student",
    "quality_audit": "Quality Assurance",
}

ROLE_DASHBOARD_URLS = {
    "system_admin": "dashboards:ict",
    "registrar": "dashboards:registrar",
    "teacher": "dashboards:teacher",
    "principal": "dashboards:principal",
    # Same dashboard as the High School Principal: the campus split changes
    # which data is loaded, not the screen the person sees.
    "elementary_principal": "dashboards:principal",
    "assistant_principal": "dashboards:academic_coordinator",
    "student": "dashboards:student",
    "quality_audit": "dashboards:quality_audit",
}


def get_user_role(user):
    """Return the role code derived from Django group memberships."""
    if not user.is_authenticated:
        return None
    keys = set(user.groups.values_list("name", flat=True))
    for code in ROLE_ORDER:
        if code in keys:
            return code
    return None


def context_get_user_role(request):
    role = get_user_role(request.user)
    email = getattr(request.user, "email", "")
    return {
        "user_role": role,
        "user_role_label": ROLE_LABELS.get(role, "User"),
        "view_dashboard_url": ROLE_DASHBOARD_URLS.get(
            role, "accounts:login_page"
        ),
        "user_display_name": getattr(request.user, "get_full_name", lambda: "")() or email,
        "user_email": email,
        "user_initials": _initials(request.user),
        "user_avatar_url": _avatar_url(request.user),
    }


def avatar_url(avatar_field):
    """Return a stable, same-origin URL path for a stored avatar field.

    Deliberately NOT ``FieldFile.url``: with a private R2 bucket that is a
    presigned URL with a one-hour expiry, so baking it into a rendered page (or
    a JSON payload the page holds on to) made profile photos disappear once the
    page aged past the signature. This path is stable and
    ``accounts.views.avatar_redirect`` signs the storage key per request.
    """
    if not avatar_field:
        return ""
    name = getattr(avatar_field, "name", "") or ""
    if not name:
        return ""
    try:
        return reverse("accounts:avatar", kwargs={"name": name})
    except NoReverseMatch:
        return ""


def _avatar_url(user):
    """The requesting user's avatar path, or "" when they have none."""
    if not getattr(user, "is_authenticated", False):
        return ""
    profile = None
    if hasattr(user, "employee_profile"):
        profile = user.employee_profile
    elif hasattr(user, "student_profile"):
        profile = user.student_profile
    return avatar_url(getattr(profile, "avatar", None))


def _initials(user):
    getter = getattr(user, "get_initials", None)
    if callable(getter):
        initials = getter()
        if initials:
            return initials
    name = getattr(user, "email", "") or ""
    parts = [p for p in name.replace("@", " ").replace(".", " ").split() if p]
    if not parts:
        return "U"
    return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()