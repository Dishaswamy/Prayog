"""
Sending emails.

If SMTP_HOST is set in .env, emails are really sent.
If not (local development), the email is printed in the uvicorn terminal
so you can copy the OTP from there.
"""

import os
import smtplib
from email.message import EmailMessage

from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "no-reply@prayog.local")



def send_email(to: str, subject: str, body: str) -> None:
    if not SMTP_HOST:
        print("\n" + "=" * 60)
        print(f"[DEV EMAIL] To: {to}\nSubject: {subject}\n\n{body}")
        print("=" * 60 + "\n")
        return

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


def send_password_reset_otp_email(to: str, otp: str, expires_minutes: int) -> None:
    send_email(
        to=to,
        subject="Prayog password reset OTP",
        body=(
            f"Your OTP to reset your Prayog password is: {otp}\n\n"
            f"It is valid for {expires_minutes} minutes and can be used once.\n"
            "If you didn't ask for this, ignore this email. Your password won't change."
        ),
    )
