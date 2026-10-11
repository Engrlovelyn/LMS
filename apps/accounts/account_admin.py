"""Administrator account-recovery actions.

Both recovery paths a school actually needs live here so the server-rendered
forms (``apps.accounts.management_views``) and the JSON endpoints the System
Admin dashboard calls (``apps.api.views``) cannot drift apart:

``unlock_user_account``
    Clear a lockout without touching the password. The user still knows (or can
    still receive) their own password, they just ran past the failed-attempt
    threshold -- most often a fat-fingered password on a phone keyboard.

``issue_temporary_password``
    The forgot-password fallback for when the self-service emailed reset link
    cannot be used (no reachable mailbox, the SMTP relay down, the link
    expired, the student never received it). The administrator hands over a
    password out-of-band and the user sets their own at next sign-in.

Every function here writes an ``ActivityLog`` row, so both the dashboard's
recent-activity feed and the school's audit trail see who did what.
"""

import secrets
import string

from django.contrib.auth.hashers import make_password
from django.utils import timezone

from apps.system_settings.models import PasswordPolicy

from .models import ActivityLog, PasswordHistory, User

# Ambiguous glyphs (0/O, 1/l/I) are left out: these passwords get read aloud
# over the phone or copied off a screen by a student, and "0" vs "O" is the
# single most common reason a handed-over password bounces on first login.
_TEMP_PASSWORD_ALPHABET = (
    string.ascii_lowercase.replace("l", "").replace("o", "")
    + string.ascii_uppercase.replace("I", "").replace("O", "")
    + string.digits.replace("0", "").replace("1", "")
    + "!@#$%&*?"
)

# A handed-over password is a known quantity, so it should not be trivially
# guessable from the address itself. 14 chars over a 79-symbol alphabet is well
# past anything a student would brute-force while standing at the front desk.
TEMP_PASSWORD_LENGTH = 14

# Statuses that stop the user signing in at all. Issuing a temporary password
# clears the ones a password reset can legitimately resolve; it deliberately
# leaves ARCHIVED alone, because reactivating a graduated/withdrawn account is a
# separate decision the administrator has to make explicitly.
_PASSWORD_RESETTABLE_STATUSES = (
    User.Status.LOCKED,
    User.Status.PENDING_PASSWORD_SETUP,
    User.Status.PENDING_VERIFICATION,
)


class AccountActionError(Exception):
    """Raised when a recovery action cannot be carried out.

    Carries a message written for the administrator reading the screen, so the
    JSON API can return it verbatim and the form views can attach it as a form
    error without re-deriving the reason.
    """


def generate_temporary_password(length=TEMP_PASSWORD_LENGTH):
    """Return a random password that satisfies the configured password policy."""
    policy = PasswordPolicy.get_solo()
    length = max(int(length or 0), int(policy.minimum_length or 0), TEMP_PASSWORD_LENGTH)

    while True:
        candidate = "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(length))
        if _policy_violations(candidate, policy):
            continue
        # Guarantee one of each class the policy demands, whatever the random
        # draw produced. Done by swapping characters in place rather than
        # shuffling so the result stays as unpredictable as the original.
        required = []
        if policy.require_lowercase:
            required.append(string.ascii_lowercase.replace("l", "").replace("o", ""))
        if policy.require_uppercase:
            required.append(string.ascii_uppercase.replace("I", "").replace("O", ""))
        if policy.require_number:
            required.append(string.digits.replace("0", "").replace("1", ""))
        if policy.require_symbol:
            required.append("!@#$%&*?")
        chars = list(candidate)
        for index, source in enumerate(required):
            if not any(c in source for c in chars):
                chars[index] = secrets.choice(source)
        password = "".join(chars)
        if not _policy_violations(password, policy):
            return password


def _policy_violations(value, policy=None):
    """Return the list of policy rules the given plaintext password breaks."""
    policy = policy or PasswordPolicy.get_solo()
    problems = []
    if len(value) < policy.minimum_length:
        problems.append("at least {} characters".format(policy.minimum_length))
    if policy.require_uppercase and not any(c.isupper() for c in value):
        problems.append("an uppercase letter")
    if policy.require_lowercase and not any(c.islower() for c in value):
        problems.append("a lowercase letter")
    if policy.require_number and not any(c.isdigit() for c in value):
        problems.append("a number")
    if policy.require_symbol and not any(not c.isalnum() for c in value):
        problems.append("a symbol")
    return problems


def lock_user_account(user, reason="Failed sign-in attempts reached the lockout threshold"):
    """Put an account into the locked state and stamp when it happened.

    Kept next to the recovery actions so the lock and the unlock agree on the
    fields they own (``status``, ``failed_login_attempts``, ``locked_at``).
    """
    user.status = User.Status.LOCKED
    user.locked_at = timezone.now()
    user.save(update_fields=["status", "locked_at", "is_active"])
    # Locking ends the current device so the single-session slot is not held
    # by an account that can no longer authenticate.
    from .session_guard import release_all_sessions

    release_all_sessions(user)
    ActivityLog.objects.create(
        user=user,
        action="account_locked",
        related_entity_type="User",
        related_entity_id=user.pk,
    )
    return reason


def unlock_user_account(user, actor=None):
    """Clear a lockout, leaving the password untouched.

    ``actor`` is the administrator performing the action. It is recorded on the
    activity log; ``user=None`` on the log row means "system", so the actor's
    name is folded into the action text to keep the who visible either way.

    Returns the number of failed attempts that were cleared.
    """
    if user.status != User.Status.LOCKED:
        raise AccountActionError(
            "{} is not locked (current status: {}).".format(
                user.email, user.get_status_display()
            )
        )

    cleared_attempts = user.failed_login_attempts
    user.status = User.Status.ACTIVE
    user.failed_login_attempts = 0
    user.locked_at = None
    # An unlocked account must be able to sign in, whatever else was pending.
    user.must_change_password = False
    user.save(
        update_fields=[
            "status",
            "failed_login_attempts",
            "locked_at",
            "must_change_password",
            "is_active",
        ]
    )
    # Clear any device row left behind while the account was locked, so the
    # user is not told "already signed in" the moment they try again.
    from .session_guard import release_all_sessions

    release_all_sessions(user)
    ActivityLog.objects.create(
        user=actor,
        action="account_unlocked",
        related_entity_type="User",
        related_entity_id=user.pk,
    )
    return cleared_attempts


def issue_temporary_password(user, new_password=None, actor=None):
    """Set a password the administrator hands to the user, and unlock the account.

    The account is left ``ACTIVE`` so the user can actually sign in with the new
    password, and ``must_change_password`` is set so the very next request is
    forced through the change-password screen. The admin-set password is
    therefore never a password the user keeps.

    Reuse of a recent password is deliberately *not* blocked here. The reuse
    limit protects a user from cycling back to their own old password; an
    administrator recovering an account has to be able to set whatever the user
    agrees to right now, and telling them "pick something different from three
    months ago" helps nobody.

    Returns a dict with the generated password and what changed, so the caller
    can show the administrator exactly what to tell the user.
    """
    if not new_password:
        new_password = generate_temporary_password()

    violations = _policy_violations(new_password)
    if violations:
        raise AccountActionError(
            "Password must include " + ", ".join(violations) + "."
        )

    previous_status = user.status
    was_locked = previous_status == User.Status.LOCKED

    user.password = make_password(new_password)
    user.password_changed_at = timezone.now()
    user.failed_login_attempts = 0
    user.locked_at = None
    user.must_change_password = True

    if previous_status in _PASSWORD_RESETTABLE_STATUSES:
        user.status = User.Status.ACTIVE

    user.save(
        update_fields=[
            "password",
            "password_changed_at",
            "failed_login_attempts",
            "locked_at",
            "must_change_password",
            "status",
            "is_active",
        ]
    )

    # Recorded so the user's own later password change is not rejected as a
    # reuse of the password an administrator set on their behalf.
    PasswordHistory.objects.create(user=user, hashed_password=user.password)
    # A new password invalidates any device still holding the old one.
    from .session_guard import release_all_sessions

    release_all_sessions(user)
    ActivityLog.objects.create(
        user=actor,
        action="admin_password_reset",
        related_entity_type="User",
        related_entity_id=user.pk,
    )

    warnings = []
    if previous_status == User.Status.ARCHIVED:
        warnings.append(
            "This account is archived, so the new password cannot be used to "
            "sign in until the account is reactivated."
        )
    if previous_status == User.Status.PENDING_VERIFICATION:
        warnings.append(
            "This account had an unverified email address; it is now active."
        )

    return {
        "password": new_password,
        "was_locked": was_locked,
        "previous_status": previous_status,
        "must_change_password": True,
        "warnings": warnings,
    }


def locked_accounts_queryset(limit=6):
    """Locked accounts, most recently locked first.

    ``locked_at`` is null on accounts locked before the column existed, so
    ``email`` breaks the tie and the ordering stays stable between renders.
    """
    qs = User.objects.filter(status=User.Status.LOCKED).select_related(
        "employee_profile", "student_profile"
    )
    if limit:
        qs = qs.order_by("-locked_at", "email")[:limit]
    else:
        qs = qs.order_by("-locked_at", "email")
    return qs


def lockout_threshold():
    """The configured failed-attempt limit, or the built-in default."""
    from apps.system_settings.models import SystemConfiguration

    try:
        return SystemConfiguration.get_solo().account_lockout_threshold or 5
    except Exception:
        return 5


def serialize_locked_account(user, can_unlock=False, can_reset=False):
    """Render one locked account for the recovery UI.

    Shared by the System Admin dashboard context and the JSON bridge so the two
    cannot describe the same lockout differently. The dashboard adds the avatar
    gradient on top; the frontend falls back to a neutral colour without it.
    """
    return {
        "id": user.pk,
        "name": user.get_full_name() or user.email,
        "email": user.email,
        "isStudent": hasattr(user, "student_profile"),
        # The lock is applied by the failed-attempt policy in the login view,
        # not by a human, so say that rather than blaming an administrator.
        "reason": "{} failed sign-in attempt(s) (limit {})".format(
            user.failed_login_attempts, lockout_threshold()
        ),
        # locked_at, not password_changed_at: the latter is when the password
        # last changed, which for a locked account is usually months ago and
        # made every row read as locked long ago.
        "when": (
            timezone.localtime(user.locked_at).strftime("%I:%M %p · %b %d, %Y")
            if user.locked_at
            else "Lock time not recorded"
        ),
        "locked_at": user.locked_at.isoformat() if user.locked_at else None,
        "failed_attempts": user.failed_login_attempts,
        "initials": user.get_initials(),
        "canUnlock": can_unlock,
        "canReset": can_reset,
    }
