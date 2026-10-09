"""
Branded HTML/plain-text email helpers for SVI LMS.

All outbound mail (verification, password reset, new-device sign-in, and the
manual resend actions) is delivered through the Django mail backend configured
in settings.EMAIL_BACKEND - Gmail SMTP by default. This module only builds the
branded bodies and sends them via ``send_html_email``; it has no transport of
its own. (The old Brevo REST-API backend was removed; key an SMTP relay in
``settings.EMAIL_*`` instead.)
"""

from __future__ import annotations

import os

from django.conf import settings

from django.core.mail import EmailMultiAlternatives


def send_html_email(
    subject,
    to_email,
    to_name="",
    html=None,
    text=None,
    from_email=None,
    logo_cid=None,
):
    """Send an HTML email through the configured Django mail backend.

    Works with the console backend too (prints the HTML), so development
    behaves identically whether or not a real relay is configured.

    ``logo_cid`` is the path to the SVI logo; any ``cid:<logo>`` references in
    the HTML are swapped for a working src (settings.EMAIL_LOGO_URL, or a
    base64 data URI) because Gmail does not render inline Content-ID images.
    """
    message = EmailMultiAlternatives(
        subject=subject,
        body=text or "Please view this email in an HTML-capable client.",
        from_email=from_email or getattr(settings, "DEFAULT_FROM_EMAIL", ""),
        to=[to_email],
    )
    if html:
        html = _inject_logo(html, logo_cid)
        message.attach_alternative(html, "text/html")
    return message.send(fail_silently=False)


def svi_logo_path():
    candidates = (
        os.path.join(getattr(settings, "BASE_DIR", ""), "static", "dashboards", "img", "SVI_logo.png"),
        os.path.join(getattr(settings, "BASE_DIR", ""), "static", "accounts", "img", "SVI_logo.png"),
        os.path.join(os.getcwd(), "static", "dashboards", "img", "SVI_logo.png"),
    )
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def svi_logo_src():
    """An <img src> value for the SVI logo.

    Priority:
      1. settings.EMAIL_LOGO_URL — a durable public HTTPS URL (recommended;
         Gmail renders external https images, not data URIs).
      2. A base64 data URI built from the local logo file.
    """
    url = (os.getenv("EMAIL_LOGO_URL") or getattr(settings, "EMAIL_LOGO_URL", "") or "").strip()
    if url:
        return url
    path = svi_logo_path()
    if path and os.path.exists(path):
        import base64

        with open(path, "rb") as logo_file:
            b64 = base64.b64encode(logo_file.read()).decode("ascii")
        return "data:image/png;base64," + b64
    return ""


def _inject_logo(html, logo_cid):
    """Replace Gmail-unsupported cid: logo references with a working src.

    Gmail does not render inline (Content-ID) images, so the logo must be
    referenced by an https URL or embedded as a data URI.
    """
    if not html or not logo_cid:
        return html
    src = svi_logo_src()
    if not src:
        return html
    name = os.path.basename(logo_cid)
    stem, _ext = os.path.splitext(name)
    return (
        html.replace('src="cid:%s"' % name, 'src="%s"' % src)
        .replace('src="cid:%s"' % stem, 'src="%s"' % src)
        .replace("src='cid:%s'" % name, 'src="%s"' % src)
        .replace("src='cid:%s'" % stem, 'src="%s"' % src)
    )


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
    """
    school = context.get("school_name") or "San Vicente Institute"
    contact = context.get("contact_email") or "info@whitestrings.io"
    name = context.get("greeting_name") or context.get("login_email") or "there"
    login_email = context.get("login_email") or ""
    login_password = context.get("login_password") or ""
    # A resend cannot include the password (only its hash is stored), so fall
    # back to telling the user to keep the one they already have.
    password_display = login_password or "(unchanged &ndash; use your current password)"
    verify_url = context.get("verify_url") or "#"
    hours = context.get("expiry_hours", 48)

    pitch = "Thank you for choosing us to be part of your career."

    brand = "#1e5faf"   # SVI blue
    accent = "#3a86d4"  # SVI sky
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{school} – Verify your email address</title>
</head>
<body style="margin:0;padding:0;background-color:#eef2f7;font-family:'Segoe UI',Arial,Helvetica,sans-serif;color:#1f2937;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;background-color:#eef2f7;">
  <tr><td align="center" style="padding:28px 12px;">
    <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:560px;max-width:100%;background-color:#ffffff;border:1px solid #d8e2ee;border-radius:12px;overflow:hidden;">
      <tr>
        <td style="background:linear-gradient(135deg,{brand} 0%,{accent} 55%,{brand} 100%);padding:22px 32px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="vertical-align:middle;width:1%;">
                <img src="cid:SVI_logo.png" alt="{school} logo" width="52"
                     style="display:inline-block;background:#ffffff;border-radius:10px;padding:4px;">
              </td>
              <td style="vertical-align:middle;padding-left:16px;color:#ffffff;">
                <div style="font-size:22px;font-weight:800;letter-spacing:.5px;line-height:1.1;">SVI LMS</div>
                <div style="font-size:12px;opacity:.88;letter-spacing:.8px;margin-top:2px;">VERIFY YOUR EMAIL ADDRESS</div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
      <tr><td style="padding:28px 32px 8px 32px;font-size:15px;line-height:1.6;">
        <p style="margin:0 0 14px 0;font-size:20px;font-weight:800;color:{brand};">Welcome to SVI, {name}!</p>
        <p style="margin:0 0 12px 0;"><strong>Your account was successfully created!</strong></p>
        <p style="margin:0 0 18px 0;">{pitch}</p>
"""
    html += f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f8fc;border:1px solid #d8e2ee;border-radius:8px;margin:0 0 20px 0;">
          <tr><td style="padding:14px 18px;font-size:12px;font-weight:800;letter-spacing:1px;color:{brand};border-bottom:1px solid #d8e2ee;">SVI LMS LOGIN CREDENTIAL</td></tr>
          <tr><td style="padding:14px 18px;font-size:14px;">
            <p style="margin:0 0 8px 0;"><span style="color:#6b7280;font-weight:600;">Email:</span> {login_email}</p>
            <p style="margin:0;"><span style="color:#6b7280;font-weight:600;">Password:</span> {password_display}</p>
          </td></tr>
        </table>
"""
    html += f"""
        <p style="margin:0 0 20px 0;font-size:14px;">
          Click this link within <strong>{hours} hours</strong> to verify your account:
        </p>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 20px 0;">
          <tr><td style="border-radius:8px;background:linear-gradient(135deg,{brand},{accent});">
            <a href="{verify_url}" style="display:inline-block;padding:12px 28px;color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;border-radius:8px;">Verify Email</a>
          </td></tr>
        </table>
        <p style="margin:0 0 24px 0;font-size:13px;color:#6b7280;">
          If the button does not open, copy this link into your browser:<br>
          <a href="{verify_url}" style="color:{brand};word-break:break-all;">{verify_url}</a>
        </p>
        <p style="margin:0 0 6px 0;">Thanks,</p>
        <p style="margin:0 0 28px 0;font-weight:700;color:{brand};">{school}</p>
      </td></tr>
      <tr>
        <td style="background-color:#eef2f7;border-top:1px solid #d8e2ee;padding:18px 32px;font-size:11px;color:#6b7280;line-height:1.7;">
          This email is auto generated. Do not reply.<br>
          If you received this email by mistake, simply delete it.<br>
          For questions and more information, please contact:
          <a href="mailto:{contact}" style="color:{brand};">{contact}</a>
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
    school = context.get("school_name") or "San Vicente Institute"
    name = context.get("greeting_name") or "there"
    contact = context.get("contact_email") or "info@whitestrings.io"
    hours = context.get("expiry_hours", 48)
    pitch = "Thank you for choosing us to be part of your career."
    password_display = context.get("login_password") or (
        "(unchanged - use your current password)"
    )
    return (
        f"Verify your email address\n\n"
        f"WELCOME TO SVI, {name}!\n\n"
        f"Your account was successfully created!\n{pitch}\n\n"
        f"SVI LMS LOGIN CREDENTIAL\n"
        f"Email: {context.get('login_email') or ''}\n"
        f"Password: {password_display}\n\n"
        f"Click this link within {hours} hours to verify your account:\n"
        f"{context.get('verify_url') or ''}\n\n"
        f"Thanks,\n{school}\n\n"
        f"This email is auto generated. Do not reply.\n"
        f"If you received this email by mistake, simply delete it.\n"
        f"For questions and more information, please contact: {contact}\n"
    )