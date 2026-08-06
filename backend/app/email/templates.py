from __future__ import annotations

from html import escape

from app.email.backends import OutboundEmail


def _action_email(
    *,
    to: str,
    subject: str,
    eyebrow: str,
    heading: str,
    message: str,
    button_label: str,
    action_url: str,
    expiry: str,
    ignore_message: str,
) -> OutboundEmail:
    safe_subject = escape(subject)
    safe_eyebrow = escape(eyebrow)
    safe_heading = escape(heading)
    safe_message = escape(message)
    safe_button_label = escape(button_label)
    safe_action_url = escape(action_url, quote=True)
    safe_expiry = escape(expiry)
    safe_ignore_message = escape(ignore_message)

    text = (
        f"Kaede Chat\n\n{heading}\n\n{message}\n\n"
        f"{button_label}: {action_url}\n\n{expiry}\n\n{ignore_message}"
    )
    html = f"""\
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="light">
    <title>{safe_subject}</title>
  </head>
  <body style="margin:0;padding:0;background:#f3efe8;color:#28231f;">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;">
      {safe_message} {safe_expiry}
    </div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
      style="width:100%;background:#f3efe8;">
      <tr>
        <td align="center" style="padding:40px 16px;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
            style="width:100%;max-width:600px;border:1px solid #d2c9bd;
              border-radius:24px;background:#fffdf9;overflow:hidden;
              box-shadow:0 16px 42px rgba(49,36,24,0.12);">
            <tr>
              <td style="padding:28px 32px;background:#211e1b;color:#fffaf3;">
                <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                  <tr>
                    <td style="width:44px;height:44px;border-radius:14px 14px 14px 5px;
                      background:#b83b26;color:#fffaf3;font-family:Arial,sans-serif;
                      font-size:20px;font-weight:800;text-align:center;
                      vertical-align:middle;">K</td>
                    <td style="padding-left:14px;font-family:Arial,sans-serif;
                      font-size:20px;font-weight:700;letter-spacing:-0.3px;">Kaede Chat</td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:42px 32px 36px;font-family:Arial,sans-serif;">
                <p style="margin:0 0 12px;color:#922916;font-size:12px;font-weight:800;
                  letter-spacing:1.5px;text-transform:uppercase;">{safe_eyebrow}</p>
                <h1 style="margin:0 0 18px;color:#28231f;font-size:34px;
                  line-height:1.12;letter-spacing:-1px;">{safe_heading}</h1>
                <p style="margin:0 0 28px;color:#5f574f;font-size:16px;
                  line-height:1.65;">{safe_message}</p>
                <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                  <tr>
                    <td style="border-radius:12px;background:#b83b26;">
                      <a href="{safe_action_url}"
                        style="display:inline-block;padding:14px 22px;color:#fffaf3;
                          font-size:16px;font-weight:700;line-height:1;
                          text-decoration:none;">{safe_button_label}</a>
                    </td>
                  </tr>
                </table>
                <p style="margin:24px 0 0;color:#756c63;font-size:13px;
                  line-height:1.6;">{safe_expiry}</p>
                <p style="margin:12px 0 0;color:#756c63;font-size:13px;line-height:1.6;">
                  If the button does not work, copy this address into your browser:<br>
                  <a href="{safe_action_url}"
                    style="color:#922916;word-break:break-all;">{safe_action_url}</a>
                </p>
              </td>
            </tr>
            <tr>
              <td style="padding:22px 32px;border-top:1px solid #e5ddd3;
                background:#f8f4ee;color:#756c63;font-family:Arial,sans-serif;
                font-size:12px;line-height:1.6;">
                {safe_ignore_message}<br>
                Kaede Chat · Open communities, on your terms.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""
    return OutboundEmail(to=to, subject=subject, text=text, html=html)


def verification_email(
    *, to: str, app_url: str, token: str, expires_in_hours: int
) -> OutboundEmail:
    return _action_email(
        to=to,
        subject="Verify your Kaede Chat account",
        eyebrow="Email verification",
        heading="Finish creating your account.",
        message="Confirm this email address to start using your Kaede Chat account.",
        button_label="Verify email",
        action_url=f"{app_url.rstrip('/')}/verify#token={token}",
        expiry=f"This link expires in {expires_in_hours} hours and can be used only once.",
        ignore_message="If you did not create this account, you can safely ignore this email.",
    )


def password_reset_email(
    *, to: str, app_url: str, token: str, expires_in_minutes: int
) -> OutboundEmail:
    return _action_email(
        to=to,
        subject="Reset your Kaede Chat password",
        eyebrow="Account recovery",
        heading="Choose a new password.",
        message="Use the secure link below to reset the password for your Kaede Chat account.",
        button_label="Reset password",
        action_url=f"{app_url.rstrip('/')}/reset-password#token={token}",
        expiry=f"This link expires in {expires_in_minutes} minutes and can be used only once.",
        ignore_message="If you did not request a password reset, you can safely ignore this email.",
    )


def email_change_confirmation(
    *, to: str, app_url: str, token: str, expires_in_minutes: int = 30
) -> OutboundEmail:
    return _action_email(
        to=to,
        subject="Confirm your new Kaede Chat email",
        eyebrow="Email change",
        heading="Confirm your new address.",
        message="Confirm that you want to use this address for your Kaede Chat account.",
        button_label="Confirm email",
        action_url=f"{app_url.rstrip('/')}/verify-email-change#token={token}",
        expiry=f"This link expires in {expires_in_minutes} minutes and can be used only once.",
        ignore_message=(
            "If you did not request this change, keep using your current email and review "
            "your account security."
        ),
    )
