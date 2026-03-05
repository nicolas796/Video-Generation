"""Email service using SendGrid for transactional emails."""
import logging
from typing import Optional

from flask import current_app

logger = logging.getLogger(__name__)


def _get_sendgrid_client():
    """Lazily import and return a SendGrid client, or None if not configured."""
    api_key = current_app.config.get('SENDGRID_API_KEY')
    if not api_key:
        return None
    from sendgrid import SendGridAPIClient
    return SendGridAPIClient(api_key)


def send_invitation_email(
    to_email: str,
    brand_name: str,
    invited_by: str,
    role: str,
    accept_url: str,
) -> bool:
    """Send a brand workspace invitation email.

    Returns True on success, False on failure (logged, never raises).
    """
    client = _get_sendgrid_client()
    from_email = current_app.config.get('SENDGRID_FROM_EMAIL', 'noreply@example.com')
    from_name = current_app.config.get('SENDGRID_FROM_NAME', 'Video Generator')

    subject = f"You've been invited to join {brand_name}"
    html_body = _invitation_html(brand_name, invited_by, role, accept_url)
    plain_body = (
        f"{invited_by} has invited you to join the \"{brand_name}\" workspace "
        f"as a {role}.\n\n"
        f"Accept your invitation here: {accept_url}\n\n"
        f"This link expires in 7 days."
    )

    if not client:
        # No SendGrid key — log the invitation link so the admin can share it manually
        logger.warning(
            "SENDGRID_API_KEY not set — invitation not emailed. "
            "Share this link manually: %s", accept_url,
        )
        return True  # treat as success so the invitation record is created

    try:
        from sendgrid.helpers.mail import Mail, Email, To, Content, HtmlContent

        message = Mail(
            from_email=Email(from_email, from_name),
            to_emails=To(to_email),
            subject=subject,
        )
        message.add_content(Content("text/plain", plain_body))
        message.add_content(Content("text/html", html_body))

        response = client.send(message)
        logger.info("Invitation email sent to %s (status %s)", to_email, response.status_code)
        return 200 <= response.status_code < 300
    except Exception:
        logger.exception("Failed to send invitation email to %s", to_email)
        return False


def _invitation_html(brand_name: str, invited_by: str, role: str, accept_url: str) -> str:
    """Render a simple, inline-styled HTML invitation email."""
    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;background:#f4f5f7;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:40px 0;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,0.08);overflow:hidden;">
  <tr><td style="background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:32px 40px;text-align:center;">
    <h1 style="color:#fff;margin:0;font-size:22px;">You&rsquo;re Invited!</h1>
  </td></tr>
  <tr><td style="padding:32px 40px;">
    <p style="font-size:16px;color:#333;line-height:1.6;margin:0 0 16px;">
      <strong>{invited_by}</strong> has invited you to join the
      <strong>{brand_name}</strong> workspace as a <strong>{role}</strong>.
    </p>
    <p style="font-size:14px;color:#666;line-height:1.5;margin:0 0 28px;">
      Click the button below to accept the invitation and start collaborating.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center">
      <a href="{accept_url}"
         style="display:inline-block;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
                color:#fff;text-decoration:none;padding:14px 36px;border-radius:8px;
                font-weight:600;font-size:16px;">
        Accept Invitation
      </a>
    </td></tr>
    </table>
    <p style="font-size:12px;color:#999;margin:24px 0 0;text-align:center;">
      This invitation expires in 7 days.
    </p>
  </td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""
