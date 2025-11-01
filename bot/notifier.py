import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate
from .config import settings
def send_trade_email(subject: str, body: str):
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_pass):
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_user
        msg["To"] = settings.smtp_user
        msg["Date"] = formatdate(localtime=True)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as s:
            s.starttls()
            s.login(settings.smtp_user, settings.smtp_pass)
            s.sendmail(settings.smtp_user, [settings.smtp_user], msg.as_string())
    except Exception as e:
        # Don't crash the bot if email fails (e.g., daily limit exceeded)
        print(f"⚠️ Email notification failed: {e}")
