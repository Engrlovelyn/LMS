"""Verify that verification/activation email can actually leave this server.

    python manage.py test_email [recipient@example.com]

With no argument it reports transport configuration without sending anything.
With a recipient it sends a real test message through the configured Django
mail backend (Gmail SMTP by default), so a bad sender, invalid app password,
or a blocked outbound port shows up here instead of silently swallowing a new
user's activation mail.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.email_branding import (
    svi_logo_src,
    verification_email_html,
    verification_email_text,
)


class Command(BaseCommand):
    help = "Check email configuration and optionally send a real test mail."

    def add_arguments(self, parser):
        parser.add_argument(
            "recipient",
            nargs="?",
            default="",
            help="Send a real test message to this address.",
        )
        parser.add_argument(
            "--token",
            default="diagnostic-only",
            help="Verification token placed in the test link.",
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING("Email transport"))
        self.stdout.write(f"  EMAIL_BACKEND      : {settings.EMAIL_BACKEND}")
        self.stdout.write(f"  DEFAULT_FROM_EMAIL : {settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"  EMAIL_HOST         : {settings.EMAIL_HOST}:{settings.EMAIL_PORT} (TLS={settings.EMAIL_USE_TLS})")
        has_user = bool(getattr(settings, "EMAIL_HOST_USER", ""))
        self.stdout.write(
            f"  SMTP credentials   : {'set' if has_user else 'NOT SET - mail will fail'}"
        )
        self.stdout.write(f"  EMAIL_LOGO_URL     : {'set' if svi_logo_src() else 'missing (logo embedded as data URI if the file exists)'}")

        recipient = (options.get("recipient") or "").strip()
        if not recipient:
            if not has_user:
                raise CommandError(
                    "No SMTP credentials are configured (set GMAIL_USER + "
                    "GMAIL_APP_PASSWORD in .env). New users would be created "
                    "but never receive their verification mail."
                )
            self.stdout.write(
                self.style.SUCCESS(
                    "\nSMTP credentials configured. Pass a recipient to send a real test mail."
                )
            )
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"Sending test mail to {recipient}"))
        base = getattr(settings, "SITE_URL", "") or "http://localhost:8000"
        verify_url = f"{base}/accounts/verify/{options.get('token') or 'diagnostic-only'}/"
        context = {
            "greeting_name": recipient.split("@")[0],
            "login_email": recipient,
            "login_password": "diagnostic-only",
            "verify_url": verify_url,
            "expiry_hours": 48,
            "school_name": getattr(settings, "SCHOOL_NAME", "San Vicente Institute"),
            "contact_email": getattr(settings, "SCHOOL_CONTACT_EMAIL", ""),
        }
        from apps.accounts.email_branding import send_html_email

        try:
            send_html_email(
                subject="[SVI LMS] Test email - please ignore",
                to_email=recipient,
                html=verification_email_html(context),
                text=verification_email_text(context),
                logo_cid="SVI_logo.png",
            )
        except Exception as exc:
            raise CommandError(f"Send failed: {exc}")

        self.stdout.write(self.style.SUCCESS(f"Sent at {timezone.now():%Y-%m-%d %H:%M}. Check the inbox and spam folder."))