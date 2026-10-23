from unittest.mock import MagicMock
import smtplib

import pytest

from app.core.config import get_settings
from app.utils import email


@pytest.mark.parametrize("port", [587, 465])
def test_reset_email_uses_configured_secure_transport(monkeypatch, port):
    settings = get_settings().model_copy(update={
        "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": port,
        "SMTP_USER": "mail@example.com", "SMTP_PASSWORD": "test app password",
        "SMTP_FROM_EMAIL": "", "SMTP_TIMEOUT_SECONDS": 7,
    })
    monkeypatch.setattr(email, "get_settings", lambda: settings)
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = smtp.return_value
    smtp.return_value.send_message.return_value = {}
    monkeypatch.setattr(email.smtplib, "SMTP_SSL" if port == 465 else "SMTP", smtp)

    assert email.send_password_reset_email("recipient@example.com", "123456") is True
    assert smtp.call_args.kwargs["timeout"] == 7
    if port == 587:
        assert smtp.return_value.starttls.call_args.kwargs["context"].check_hostname
    else:
        assert smtp.call_args.kwargs["context"].check_hostname
        smtp.return_value.starttls.assert_not_called()
    smtp.return_value.login.assert_called_once_with("mail@example.com", "testapppassword")
    message = smtp.return_value.send_message.call_args.args[0]
    assert message["From"] == "mail@example.com"
    assert message["To"] == "recipient@example.com"
    assert "Reset Your Password" in message["Subject"]


def test_email_login_failure_returns_failure_without_logging_secrets(monkeypatch, caplog):
    smtp = MagicMock(side_effect=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
    monkeypatch.setattr(email, "smtp_connection", smtp)
    assert email.send_password_reset_email("recipient@example.com", "123456") is False
    assert "SMTPAuthenticationError" in caplog.text
    assert "535" in caplog.text
    assert "123456" not in caplog.text
    assert "recipient@example.com" not in caplog.text
