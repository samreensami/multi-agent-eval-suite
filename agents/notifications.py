"""Email notifications for high-risk evaluation results."""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def send_email_alert(task_id: str, status: str, summary: str) -> bool:
    """Send an anomaly alert using SMTP settings from environment variables.

    Required settings: SMTP_HOST, SMTP_FROM, ALERT_EMAIL_TO.
    Optional settings: SMTP_PORT (587), SMTP_USERNAME, SMTP_PASSWORD,
    SMTP_USE_TLS (true), and SMTP_TIMEOUT_SECONDS (10).

    Returns False when email is not configured. SMTP and configuration errors
    are raised so the caller can report them without hiding delivery failures.
    """
    host = os.getenv("SMTP_HOST")
    sender = os.getenv("SMTP_FROM")
    recipients = os.getenv("ALERT_EMAIL_TO")
    if not host or not sender or not recipients:
        logger.warning(
            "Email alert skipped because SMTP_HOST, SMTP_FROM, or ALERT_EMAIL_TO is unset."
        )
        return False

    try:
        port = int(os.getenv("SMTP_PORT", "587"))
        timeout = float(os.getenv("SMTP_TIMEOUT_SECONDS", "10"))
    except ValueError as exc:
        raise ValueError("SMTP_PORT and SMTP_TIMEOUT_SECONDS must be numeric.") from exc
    if port < 1 or port > 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535.")
    if timeout <= 0:
        raise ValueError("SMTP_TIMEOUT_SECONDS must be greater than zero.")

    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    if bool(username) != bool(password):
        raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be set together.")

    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower()
    if use_tls not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError("SMTP_USE_TLS must be true or false.")
    enable_tls = use_tls in {"true", "1", "yes"}

    message = EmailMessage()
    message["Subject"] = f"[Multi-Agent Eval] {status}: {task_id}"
    message["From"] = sender
    message["To"] = recipients
    message.set_content(
        f"Task ID: {task_id}\n"
        f"Status: {status}\n"
        f"Summary: {summary}\n"
    )

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.ehlo()
        if enable_tls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(message)

    logger.info(
        "Anomaly email alert sent",
        extra={"task_id": task_id, "status": status},
    )
    return True
