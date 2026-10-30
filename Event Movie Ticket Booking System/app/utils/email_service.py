import os
import smtplib
import logging

from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def send_email(
    recipient: str,
    subject: str,
    body: str,
    attachments: list[str] | None = None,
) -> bool:
    """
    Send an email using SMTP.

    Optional attachments can contain paths to generated PDF tickets.
    Returns True when sent successfully, otherwise False.
    """
    enabled = os.getenv("SMTP_ENABLED", "false").lower() == "true"

    if not enabled:
        logger.info(
            "SMTP disabled; email skipped for %s: %s",
            recipient,
            subject,
        )
        return False

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")

    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        logger.error("SMTP_PORT must be a valid integer")
        return False

    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM_EMAIL", username)

    if not username or not password or not sender:
        logger.error("SMTP credentials are missing")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)

    # Attach files such as the generated PDF ticket.
    if attachments:
        for file_path in attachments:
            try:
                with open(file_path, "rb") as file:
                    file_data = file.read()

                filename = os.path.basename(file_path)

                if filename.lower().endswith(".pdf"):
                    maintype, subtype = "application", "pdf"
                else:
                    maintype, subtype = "application", "octet-stream"

                message.add_attachment(
                    file_data,
                    maintype=maintype,
                    subtype=subtype,
                    filename=filename,
                )

            except (OSError, ValueError):
                logger.exception(
                    "Could not attach file %s to email",
                    file_path,
                )
                return False

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(message)

        logger.info("Email sent to %s", recipient)
        return True

    except (smtplib.SMTPException, OSError, ValueError):
        logger.exception("Email could not be sent to %s", recipient)
        return False

