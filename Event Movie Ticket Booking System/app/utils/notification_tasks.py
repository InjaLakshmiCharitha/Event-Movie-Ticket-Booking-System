import logging
from fastapi import BackgroundTasks

from app.utils.email_service import send_email

logger = logging.getLogger(__name__)


def queue_email(
    background_tasks: BackgroundTasks,
    recipient: str,
    subject: str,
    body: str,
    attachments: list[str] | None = None,
) -> None:
    """Queue an email to run after the API response is sent."""
    background_tasks.add_task(
        send_email,
        recipient,
        subject,
        body,
        attachments,
    )


def send_booking_confirmation_email(
    background_tasks: BackgroundTasks,
    recipient: str,
    booking_number: str,
    event_title: str,
    show_time: str,
    ticket_pdf_path: str | None = None,
) -> None:
    """Queue a booking confirmation with an optional PDF ticket."""
    subject = f"Booking Confirmed - {event_title}"

    body = (
        "Hello,\n\n"
        "Your ticket booking has been confirmed successfully.\n\n"
        f"Event: {event_title}\n"
        f"Booking Number: {booking_number}\n"
        f"Show Time: {show_time}\n\n"
        "Your PDF ticket is attached when available. "
        "Please keep your QR code ready at the venue entrance.\n\n"
        "Thank you for booking with us."
    )

    attachments = [ticket_pdf_path] if ticket_pdf_path else None

    queue_email(
        background_tasks,
        recipient,
        subject,
        body,
        attachments,
    )


def send_payment_failed_email(
    background_tasks: BackgroundTasks,
    recipient: str,
    booking_number: str,
) -> None:
    queue_email(
        background_tasks,
        recipient,
        "Payment Failed - Ticket Booking",
        (
            "Hello,\n\n"
            "Your payment could not be completed.\n"
            f"Booking Number: {booking_number}\n\n"
            "Please check your booking status before trying again."
        ),
    )


def send_cancellation_email(
    background_tasks: BackgroundTasks,
    recipient: str,
    booking_number: str,
    refund_amount: float,
    reason: str,
) -> None:
    queue_email(
        background_tasks,
        recipient,
        "Booking Cancelled - Ticket Booking",
        (
            "Hello,\n\n"
            "Your booking has been cancelled.\n"
            f"Booking Number: {booking_number}\n"
            f"Reason: {reason}\n"
            f"Refund Amount: ₹{refund_amount:.2f}\n\n"
            "Refund processing depends on the payment provider."
        ),
    )


def send_show_cancellation_email(
    background_tasks: BackgroundTasks,
    recipient: str,
    event_title: str,
    booking_number: str,
    refund_amount: float,
) -> None:
    queue_email(
        background_tasks,
        recipient,
        f"Show Cancelled - {event_title}",
        (
            "Hello,\n\n"
            f"The show for '{event_title}' has been cancelled.\n"
            f"Booking Number: {booking_number}\n"
            f"Refund Amount: ₹{refund_amount:.2f}\n\n"
            "We apologize for the inconvenience."
        ),
    )


def send_waitlist_email(
    background_tasks: BackgroundTasks,
    recipient: str,
    event_title: str,
    show_time: str,
) -> None:
    queue_email(
        background_tasks,
        recipient,
        f"Seats Available - {event_title}",
        (
            "Hello,\n\n"
            "Seats may now be available for a show you joined the waitlist for.\n"
            f"Event: {event_title}\n"
            f"Show Time: {show_time}\n\n"
            "Please open the booking system to check availability. "
            "Seats are not reserved until your booking is confirmed."
        ),
    )

