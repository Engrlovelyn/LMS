ROLE_ORDER = [
    "system_admin",
    "registrar",
    "principal",
    "assistant_principal",
    "teacher",
    "student",
]

ROLE_DASHBOARD_URLS = {
    "system_admin": "dashboards:ict",
    "registrar": "dashboards:registrar",
    "teacher": "dashboards:teacher",
    "principal": "dashboards:principal",
    "assistant_principal": "dashboards:academic_coordinator",
    "student": "dashboards:student",
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
    return {
        "user_role": role,
        "view_dashboard_url": ROLE_DASHBOARD_URLS.get(
            role, "accounts:login_page"
        ),
    }