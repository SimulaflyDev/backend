"""Run `python -m scripts.check_email` on the API host; never sends an email."""

import smtplib

from app.utils.email import smtp_connection


def main() -> int:
    try:
        with smtp_connection():
            print("SMTP connection, TLS and mailbox authentication succeeded. No email sent.")
        return 0
    except smtplib.SMTPAuthenticationError as exc:
        print(f"SMTP login rejected ({exc.smtp_code}). Update SMTP_USER/SMTP_PASSWORD on the API host.")
    except smtplib.SMTPServerDisconnected:
        print("Mail server closed the connection. Check the mailbox app password, SMTP access and API host firewall.")
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        print(f"SMTP check failed ({type(exc).__name__}). Check SMTP settings and outbound mail access on the API host.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
