"""Email sending for low-stock alerts.

Pick ONE provider by setting environment variables (see README):
  Brevo API  : BREVO_API_KEY + MAIL_FROM   (HTTPS; free plan can send from one verified address)
  Resend API : RESEND_API_KEY + MAIL_FROM  (HTTPS)
  SMTP       : SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD + MAIL_FROM
MAIL_BACKEND=memory is for automated tests only (messages are kept in OUTBOX).
"""
import os
import smtplib
import ssl
from email.message import EmailMessage

import requests

OUTBOX = []  # used only when MAIL_BACKEND=memory


def backend():
    b = os.environ.get("MAIL_BACKEND", "").strip().lower()
    if b:
        return b
    if os.environ.get("BREVO_API_KEY"):
        return "brevo"
    if os.environ.get("RESEND_API_KEY"):
        return "resend"
    if os.environ.get("SMTP_HOST"):
        return "smtp"
    return "none"


def sender():
    return os.environ.get("MAIL_FROM", "").strip()


def configured():
    b = backend()
    if b == "none":
        return False
    if b == "memory":
        return True
    return bool(sender())


def describe():
    b = backend()
    return {"backend": b, "configured": configured(), "from": sender()}


def send(recipients, subject, text):
    """Returns (ok, error_message)."""
    b = backend()
    try:
        if b == "memory":
            OUTBOX.append({"to": list(recipients), "subject": subject, "text": text})
            return True, ""
        if b == "brevo":
            r = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": os.environ["BREVO_API_KEY"], "accept": "application/json"},
                json={
                    "sender": {"email": sender(), "name": "Exam Inventory"},
                    "to": [{"email": x} for x in recipients],
                    "subject": subject,
                    "textContent": text,
                },
                timeout=20,
            )
            return (r.ok, "" if r.ok else f"Brevo error {r.status_code}: {r.text[:200]}")
        if b == "resend":
            r = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": "Bearer " + os.environ["RESEND_API_KEY"]},
                json={"from": sender(), "to": list(recipients), "subject": subject, "text": text},
                timeout=20,
            )
            return (r.ok, "" if r.ok else f"Resend error {r.status_code}: {r.text[:200]}")
        if b == "smtp":
            msg = EmailMessage()
            msg["From"] = sender()
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = subject
            msg.set_content(text)
            host = os.environ["SMTP_HOST"]
            port = int(os.environ.get("SMTP_PORT", "587"))
            user = os.environ.get("SMTP_USER", "")
            pw = os.environ.get("SMTP_PASSWORD", "")
            ctx = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
                    if user:
                        s.login(user, pw)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(host, port, timeout=20) as s:
                    if os.environ.get("SMTP_STARTTLS", "1") != "0":
                        s.starttls(context=ctx)
                    if user:
                        s.login(user, pw)
                    s.send_message(msg)
            return True, ""
        return False, "Email is not configured"
    except Exception as e:  # network/auth problems must never break a checkout
        return False, f"{type(e).__name__}: {e}"[:300]
