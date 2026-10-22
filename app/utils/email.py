import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "no-reply@simulafly.com"
SMTP_PASSWORD = "lfrk nsgy htog ejsa"  # provided by user

def send_otp_email(to_email: str, otp: str):
    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
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
    
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"OTP email sent successfully to {to_email}")
        return True
    except Exception as e:
        print(f"Error sending OTP email to {to_email}: {e}")
        return False


def send_password_reset_email(to_email: str, otp: str):
    msg = MIMEMultipart()
    msg['From'] = SMTP_USER
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

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print(f"Password reset email sent successfully to {to_email}")
        return True
    except Exception as e:
        print(f"Error sending password reset email to {to_email}: {e}")
        return False
