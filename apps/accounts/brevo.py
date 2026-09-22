"""
Brevo (Sendinblue) transactional email integration for TASS LMS.

Why REST API v3 instead of SMTP: the API key given by Brevo is an `xkeysib-`
bearer API key. SMTP relay authentication requires a *separate* SMTP login +
master password, so calling https://api.brevo.com/v3/smtp/email directly with
the bearer key is the only dependency-free way to send with what we already
have. Every Django `send_mail` / `EmailMultiAlternatives` call is routed
through `BrevoEmailBackend`, so verification, password reset and
new-device-sign-in mails all go out through Brevo once BREVO_API_KEY is set.
"""

from __future__ import annotations

import json
import os
import urllib.request

from django.conf import settings

from django.core.mail import EmailMultiAlternatives

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def _api_key():
    return os.getenv("BREVO_API_KEY") or getattr(settings, "BREVO_API_KEY", "") or ""


def _post_payload(payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BREVO_API_URL, data=data, method="POST")
    req.add_header("api-key", _api_key())
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _names(pairs):
    people = []
    for item in pairs or []:
        if isinstance(item, str):
            people.append({"email": item})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            people.append({"name": item[0], "email": item[1]})
    return people


class BrevoEmailBackend:
    """Minimal EmailBackend-compatible object sending via the Brevo REST API.

    Not a Django ``BaseEmailBackend`` subclass on purpose: the API key is a
    bearer key and there is no SMTP configuration, so Django's SMTP machinery
    never needs to bootstrap. The object exposes the two methods Django's
    email layer calls (``open`` / ``close`` / ``send_messages``).
    """

    def __init__(self, fail_silently=False, **kwargs):
        self.fail_silently = fail_silently

    def open(self):
        return True

    def close(self):
        pass

    def send_messages(self, email_messages):
        if not _api_key():
            raise RuntimeError(
                "BREVO_API_KEY is empty. Add it to .env (see settings)."
            )
        sent = 0
        for message in email_messages:
            try:
                self._send_one(message)
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
        return sent

    def _send_one(self, message):
        sender_email = message.from_email or getattr(
            settings, "DEFAULT_FROM_EMAIL", ""
        )
        sender_name = os.getenv("BREVO_SENDER_NAME") or getattr(
            settings, "BREVO_SENDER_NAME", ""
        )
        html = None
        text = message.body or ""
        for content, mimetype in (getattr(message, "alternatives", None) or ()):
            if mimetype == "text/html" and content:
                html = content

        payload = {
            "sender": {"email": sender_email, "name": sender_name},
            "to": _names(getattr(message, "to", None)) or [],
            "subject": message.subject or "",
        }
        if html:
            payload["htmlContent"] = html
        if text:
            payload["textContent"] = text
        try:
            status, body = _post_payload(payload)
        except Exception as exc:  # network / HTTP errors
            if not self.fail_silently:
                raise RuntimeError(f"Brevo send failed: {exc}") from exc
            return
        if status >= 400:
            raise RuntimeError(f"Brevo rejected mail ({status}): {body[:300]}")


def send_html_email(
    subject,
    to_email,
    to_name="",
    html=None,
    text=None,
    from_email=None,
):
    """Send an HTML email through the configured backend.

    Works with the console backend too (prints the HTML), so development
    behaves identically whether or not BREVO_API_KEY is set.
    """
    message = EmailMultiAlternatives(
        subject=subject,
        body=text or "Please view this email in an HTML-capable client.",
        from_email=from_email or getattr(settings, "DEFAULT_FROM_EMAIL", ""),
        to=[to_email],
    )
    if html:
        message.attach_alternative(html, "text/html")
    return message.send(fail_silently=False)


def verification_email_html(context):
    """The branded HTML body for account-creation + email-verification mails.

    context keys:
      greeting_name  – how the mail opens (e.g. "Hi Emelito Caleda Malanot Sr.,")
      login_email    – the account email
      login_password – the temporary/initial password
      verify_url     – absolute verification link
      expiry_hours   – link validity window (48)
      school_name    – closing "Thanks, ..." / header brand
      contact_email  – footer contact address
      account_kind   – "student" | "employee" (tweaks the pitch line)
    """
    school = context.get("school_name") or "Saint Joseph's College of Baggao"
    contact = context.get("contact_email") or "info@whitestrings.io"
    name = context.get("greeting_name") or context.get("login_email") or "there"
    login_email = context.get("login_email") or ""
    login_password = context.get("login_password") or ""
    verify_url = context.get("verify_url") or "#"
    hours = context.get("expiry_hours", 48)
    kind = context.get("account_kind") or "student"

    if kind == "employee":
        pitch = (
            "Your account has been created for the staff of "
            f"{school}. Your login credentials are below."
        )
    else:
        pitch = (
            "Thank you for choosing us to be part of your "
            "child's career and future!"
        )

    logo_initials = "".join(
        part[0] for part in school.split() if part and part[0].isupper()
    )[:2] or "TASS"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{school} – Verify your email address</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f1ec;font-family:'Segoe UI',Arial,Helvetica,sans-serif;color:#1f2937;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;background-color:#f4f1ec;">
  <tr><td align="center" style="padding:28px 12px;">
    <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:560px;max-width:100%;background-color:#ffffff;border:1px solid #e7dfd4;border-radius:12px;overflow:hidden;">
      <tr>
        <td style="background:linear-gradient(135deg,#7a1f2b 0%,#a3263a 55%,#7a1f2b 100%);padding:26px 32px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="vertical-align:middle;">
                <span style="display:inline-block;width:44px;height:44px;line-height:44px;text-align:center;background:#ffffff;border-radius:8px;font-size:18px;font-weight:800;color:#7a1f2b;letter-spacing:1px;">{logo_initials}</span>
              </td>
              <td style="vertical-align:middle;padding-left:14px;color:#ffffff;font-size:20px;font-weight:700;">Verify your email address</td>
            </tr>
          </table>
        </td>
      </tr>
      <tr><td style="padding:28px 32px 8px 32px;font-size:15px;line-height:1.6;">
        <p style="margin:0 0 12px 0;">Hi {name},</p>
        <p style="margin:0 0 12px 0;"><strong>Your account was successfully created!</strong></p>
        <p style="margin:0 0 18px 0;">{pitch}</p>
"""
    html += f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#fbf6ee;border:1px solid #e7dfd4;border-radius:8px;margin:0 0 20px 0;">
          <tr><td style="padding:14px 18px;font-size:12px;font-weight:800;letter-spacing:1px;color:#7a1f2b;border-bottom:1px solid #e7dfd4;">SIS LOGIN CREDENTIAL</td></tr>
          <tr><td style="padding:14px 18px;font-size:14px;">
            <p style="margin:0 0 8px 0;"><span style="color:#6b7280;font-weight:600;">Email:</span> {login_email}</p>
            <p style="margin:0;"><span style="color:#6b7280;font-weight:600;">Password:</span> {login_password}</p>
          </td></tr>
        </table>
"""
    html += f"""
        <p style="margin:0 0 20px 0;font-size:14px;">
          Click this link within <strong>{hours} hours</strong> to verify your account:
        </p>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 20px 0;">
          <tr><td style="border-radius:8px;background-color:#7a1f2b;">
            <a href="{verify_url}" style="display:inline-block;padding:12px 28px;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;border-radius:8px;">Verify Email</a>
          </td></tr>
        </table>
        <p style="margin:0 0 24px 0;font-size:13px;color:#6b7280;">
          If the button does not open, copy this link into your browser:<br>
          <a href="{verify_url}" style="color:#7a1f2b;word-break:break-all;">{verify_url}</a>
        </p>
        <p style="margin:0 0 6px 0;">Thanks,</p>
        <p style="margin:0 0 28px 0;font-weight:700;color:#7a1f2b;">{school}</p>
      </td></tr>
      <tr>
        <td style="background-color:#f6f1ea;border-top:1px solid #e7dfd4;padding:18px 32px;font-size:11px;color:#8b8277;line-height:1.7;">
          This email is auto generated. Do not reply.<br>
          If you received this email by mistake, simply delete it.<br>
          For questions and more information, please contact:
          <a href="mailto:{contact}" style="color:#7a1f2b;">{contact}</a>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
    return html


def verification_email_text(context):
    """Plain-text fallback mirroring the HTML content."""
    school = context.get("school_name") or "Saint Joseph's College of Baggao"
    name = context.get("greeting_name") or "there"
    contact = context.get("contact_email") or "info@whitestrings.io"
    hours = context.get("expiry_hours", 48)
    kind = context.get("account_kind") or "student"
    pitch = (
        "Your account has been created for the staff of " + school + "."
        if kind == "employee"
        else "Thank you for choosing us to be part of your child's career and future!"
    )
    return (
        f"Verify your email address\n\n"
        f"Hi {name},\n\n"
        f"Your account was successfully created!\n{pitch}\n\n"
        f"SIS LOGIN CREDENTIAL\n"
        f"Email: {context.get('login_email') or ''}\n"
        f"Password: {context.get('login_password') or ''}\n\n"
        f"Click this link within {hours} hours to verify your account:\n"
        f"{context.get('verify_url') or ''}\n\n"
        f"Thanks,\n{school}\n\n"
        f"This email is auto generated. Do not reply.\n"
        f"If you received this email by mistake, simply delete it.\n"
        f"For questions and more information, please contact: {contact}\n"
    )