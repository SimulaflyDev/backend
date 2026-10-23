import smtplib
import logging
import ssl
from contextlib import contextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@contextmanager
def smtp_connection():
    settings = get_settings()
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        raise ValueError("Configure SMTP_USER and SMTP_PASSWORD in the backend environment")
    context = ssl.create_default_context()
    if settings.SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(
            settings.SMTP_HOST, settings.SMTP_PORT,
            timeout=settings.SMTP_TIMEOUT_SECONDS, context=context,
        )
    else:
        server = smtplib.SMTP(
            settings.SMTP_HOST, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT_SECONDS,
        )
    with server:
        server.ehlo()
        if settings.SMTP_PORT != 465:
            server.starttls(context=context)
            server.ehlo()
        password = settings.SMTP_PASSWORD
        if settings.SMTP_HOST == "smtp.gmail.com":
            password = password.replace(" ", "")
        server.login(settings.SMTP_USER, password)
        yield server


def _send_email(msg: MIMEMultipart) -> bool:
    stage = "connection/authentication"
    try:
        with smtp_connection() as server:
            stage = "delivery"
            refused = server.send_message(msg)
            if refused:
                logger.error("SMTP delivery refused by server")
                return False
        return True
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        # Never log email contents, reset codes, or credentials.
        logger.error(
            "SMTP %s failed: %s (SMTP status %s). Check backend SMTP configuration and mailbox access.",
            stage, type(exc).__name__, getattr(exc, "smtp_code", "unavailable"),
        )
        return False

def send_otp_email(to_email: str, otp: str):
    msg = MIMEMultipart()
    msg['From'] = get_settings().SMTP_FROM_EMAIL or get_settings().SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = "SimulaFly - Verify Your Business Email"
    
    body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;">
          <h2 style="color: #0e9f88; text-align: center;">Verify your email</h2>
          <p>Hello,</p>
          <p>Thank you for signing up with SimulaFly. To complete your registration and verify your business email, please use the following One-Time Password (OTP):</p>
          <div style="font-size: 24px; font-weight: bold; text-align: center; letter-spacing: 4px; margin: 20px 0; color: #0e9f88;">
            {otp}
          </div>
          <p>This OTP is valid for 10 minutes. If you did not request this, please ignore this email.</p>
          <br>
          <p>Best regards,<br>The SimulaFly Team</p>
        </div>
      </body>
    </html>
    """
    msg.attach(MIMEText(body, 'html'))
    
    return _send_email(msg)


def send_password_reset_email(to_email: str, otp: str):
    msg = MIMEMultipart()
    msg['From'] = get_settings().SMTP_FROM_EMAIL or get_settings().SMTP_USER
    msg['To'] = to_email
    msg['Subject'] = "SimulaFly - Reset Your Password"

    body = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;">
          <h2 style="color: #0e9f88; text-align: center;">Reset your password</h2>
          <p>Hello,</p>
          <p>Use this one-time password (OTP) to reset your SimulaFly Merchant password:</p>
          <div style="font-size: 24px; font-weight: bold; text-align: center; letter-spacing: 4px; margin: 20px 0; color: #0e9f88;">
            {otp}
          </div>
          <p>This OTP is valid for 10 minutes. If you did not request a password reset, you can safely ignore this email.</p>
          <br>
          <p>Best regards,<br>The SimulaFly Team</p>
        </div>
      </body>
    </html>
    """
    msg.attach(MIMEText(body, 'html'))

    return _send_email(msg)
