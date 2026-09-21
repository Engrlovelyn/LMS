ROLE_ORDER = [
    "system_admin",
    "registrar",
    "principal",
    "assistant_principal",
    "teacher",
    "student",
    "tass_administrator",
    "quality_audit",
]

ROLE_LABELS = {
    "system_admin": "System Administrator",
    "registrar": "Registrar",
    "principal": "Principal",
    "assistant_principal": "Assistant Principal",
    "teacher": "Teacher",
    "student": "Student",
    "tass_administrator": "TASS Administrator",
    "quality_audit": "Quality Assurance",
}

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
        "user_initials": _initials(request.user),
    }


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