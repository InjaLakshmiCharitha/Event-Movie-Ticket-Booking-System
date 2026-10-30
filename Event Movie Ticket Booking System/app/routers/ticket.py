import os
import hmac
import hashlib
import logging

from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta

import qrcode
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image,
)

from app.database import get_db
from app.models.models import (
    User,
    Venue,
    Screen,
    Seat,
    Event,
    Show,
    Booking,
    BookingSeat,
    Payment,
)
from app.auth.dependencies import get_current_user


load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Tickets & QR Code Entry"])

TICKET_DIRECTORY = Path("generated_tickets")
PDF_DIRECTORY = TICKET_DIRECTORY / "pdf"
QR_DIRECTORY = TICKET_DIRECTORY / "qr"

PDF_DIRECTORY.mkdir(parents=True, exist_ok=True)
QR_DIRECTORY.mkdir(parents=True, exist_ok=True)

QR_SECRET = os.getenv("QR_SECRET_KEY", "")


def require_qr_secret():
    if len(QR_SECRET) < 32:
        raise HTTPException(
            status_code=500,
            detail=(
                "Configure QR_SECRET_KEY with a secret "
                "of at least 32 characters"
            ),
        )


def make_signed_qr(ticket: BookingSeat, booking: Booking) -> str:
    require_qr_secret()

    payload = f"{ticket.ticket_code}|{booking.booking_number}"

    signature = hmac.new(
        QR_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f"{payload}|{signature}"


def verify_signed_qr(qr_value: str):
    require_qr_secret()

    parts = qr_value.split("|")

    if len(parts) != 3:
        raise HTTPException(
            status_code=400,
            detail="Invalid QR code",
        )

    ticket_code, booking_number, supplied_signature = parts
    payload = f"{ticket_code}|{booking_number}"

    expected_signature = hmac.new(
        QR_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected_signature,
        supplied_signature,
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid QR signature",
        )

    return ticket_code, booking_number


def get_show_and_event(db: Session, show_id: int):
    show = db.query(Show).filter(Show.id == show_id).first()

    if not show:
        raise HTTPException(
            status_code=404,
            detail="Show not found",
        )

    event = db.query(Event).filter(
        Event.id == show.event_id
    ).first()

    if not event:
        raise HTTPException(
            status_code=404,
            detail="Event not found",
        )

    screen = db.query(Screen).filter(
        Screen.id == show.screen_id
    ).first()

    if not screen:
        raise HTTPException(
            status_code=404,
            detail="Screen not found",
        )

    venue = db.query(Venue).filter(
        Venue.id == screen.venue_id
    ).first()

    if not venue:
        raise HTTPException(
            status_code=404,
            detail="Venue not found",
        )

    return show, event, screen, venue


def check_confirmed_and_paid(
    booking: Booking,
    db: Session,
):
    if booking.status != "Confirmed":
        raise HTTPException(
            status_code=409,
            detail="Tickets are available only for Confirmed bookings",
        )

    payment = db.query(Payment).filter(
        Payment.booking_id == booking.id,
        Payment.status == "Success",
    ).first()

    if not payment:
        raise HTTPException(
            status_code=409,
            detail="Successful payment not found",
        )

    return payment


def create_qr_image(qr_value: str) -> BytesIO:
    qr = qrcode.QRCode(
        version=1,
        box_size=8,
        border=4,
    )

    qr.add_data(qr_value)
    qr.make(fit=True)

    image = qr.make_image(
        fill_color="black",
        back_color="white",
    )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)

    return buffer


def authorize_ticket_access(
    booking: Booking,
    current_user: User,
    db: Session,
):
    if current_user.role == "Admin":
        return

    if (
        current_user.role == "Customer"
        and booking.customer_id == current_user.id
    ):
        return

    if current_user.role == "Organizer":
        event = db.query(Event).filter(
            Event.id == booking.show.event_id
        ).first()

        if event and event.organizer_id == current_user.id:
            return

    raise HTTPException(
        status_code=403,
        detail="You are not allowed to access this ticket",
    )


def generate_booking_ticket_pdf(
    booking: Booking,
    db: Session,
) -> str:
    """
    Generate and save the PDF ticket.

    Returns the saved PDF path so that the same PDF can be
    downloaded by the customer or attached to an email.
    """
    check_confirmed_and_paid(booking, db)

    show, event, screen, venue = get_show_and_event(
        db,
        booking.show_id,
    )

    tickets = (
        db.query(BookingSeat)
        .filter(BookingSeat.booking_id == booking.id)
        .order_by(BookingSeat.id)
        .all()
    )

    if not tickets:
        raise HTTPException(
            status_code=404,
            detail="No tickets found",
        )

    styles = getSampleStyleSheet()

    story = [
        Paragraph(
            "EVENT & MOVIE TICKET",
            styles["Title"],
        ),
        Spacer(1, 5 * mm),
        Paragraph(
            f"Booking Number: {booking.booking_number}",
            styles["Normal"],
        ),
        Paragraph(
            f"Customer: {booking.customer.full_name}",
            styles["Normal"],
        ),
        Paragraph(
            f"Event: {event.title}",
            styles["Normal"],
        ),
        Paragraph(
            f"Venue: {venue.name}, {venue.address}",
            styles["Normal"],
        ),
        Paragraph(
            f"Screen: {screen.name}",
            styles["Normal"],
        ),
        Paragraph(
            f"Show date: {show.show_date.strftime('%Y-%m-%d')}",
            styles["Normal"],
        ),
        Paragraph(
            f"Show time: {show.start_time.strftime('%H:%M')} - "
            f"{show.end_time.strftime('%H:%M')}",
            styles["Normal"],
        ),
        Paragraph(
            f"Seat count: {booking.seat_count}",
            styles["Normal"],
        ),
        Spacer(1, 5 * mm),
    ]

    payment = db.query(Payment).filter(
        Payment.booking_id == booking.id,
        Payment.status == "Success",
    ).first()

    if not payment:
        raise HTTPException(
            status_code=409,
            detail="Successful payment not found",
        )

    story.insert(
        10,
        Paragraph(
            f"Amount paid: Rs. {payment.amount}",
            styles["Normal"],
        ),
    )

    seat_rows = [
        ["Seat", "Category", "Ticket price", "Ticket code"]
    ]

    qr_files = []

    for ticket in tickets:
        seat = db.query(Seat).filter(
            Seat.id == ticket.seat_id
        ).first()

        if not seat:
            continue

        seat_rows.append([
            f"{seat.row_label}{seat.seat_number}",
            seat.category,
            f"Rs. {ticket.price}",
            ticket.ticket_code,
        ])

        qr_value = make_signed_qr(ticket, booking)

        qr_path = QR_DIRECTORY / f"{ticket.ticket_code}.png"
        qr_buffer = create_qr_image(qr_value)
        qr_path.write_bytes(qr_buffer.getvalue())

        ticket.qr_path = str(qr_path)
        qr_files.append((ticket.ticket_code, qr_path))

    if len(seat_rows) == 1:
        raise HTTPException(
            status_code=404,
            detail="No valid seats found for this booking",
        )

    seat_table = Table(seat_rows, repeatRows=1)

    seat_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.darkblue),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))

    story.append(seat_table)
    story.append(Spacer(1, 6 * mm))

    for ticket_code, qr_path in qr_files:
        story.append(
            Paragraph(
                f"QR for seat ticket: {ticket_code}",
                styles["Heading3"],
            )
        )

        story.append(
            Image(
                str(qr_path),
                width=35 * mm,
                height=35 * mm,
            )
        )

        story.append(Spacer(1, 4 * mm))

    # Use a filename derived from the booking number.
    safe_booking_number = "".join(
        character
        for character in str(booking.booking_number)
        if character.isalnum() or character in ("-", "_")
    )

    if not safe_booking_number:
        safe_booking_number = f"booking_{booking.id}"

    pdf_path = PDF_DIRECTORY / f"{safe_booking_number}.pdf"

    document = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    document.build(story)

    db.commit()

    logger.info(
        "Ticket PDF generated for booking %s",
        booking.booking_number,
    )

    return str(pdf_path.resolve())


@router.get("/bookings/{booking_id}/ticket")
def download_booking_ticket(
    booking_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    booking = db.query(Booking).filter(
        Booking.id == booking_id
    ).first()

    if not booking:
        raise HTTPException(
            status_code=404,
            detail="Booking not found",
        )

    authorize_ticket_access(
        booking,
        current_user,
        db,
    )

    check_confirmed_and_paid(booking, db)

    pdf_path = generate_booking_ticket_pdf(
        booking,
        db,
    )

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=f"{booking.booking_number}.pdf",
    )


class TicketScanRequest(BaseModel):
    qr_value: str = Field(
        min_length=1,
        max_length=1000,
    )


@router.post("/tickets/scan")
def scan_ticket(
    data: TicketScanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "Staff":
        raise HTTPException(
            status_code=403,
            detail="Only Staff can scan tickets",
        )

    if not current_user.assigned_venue_id:
        raise HTTPException(
            status_code=403,
            detail="Staff member has no assigned venue",
        )

    ticket_code, booking_number = verify_signed_qr(
        data.qr_value
    )

    ticket = db.query(BookingSeat).filter(
        BookingSeat.ticket_code == ticket_code
    ).first()

    if not ticket:
        raise HTTPException(
            status_code=404,
            detail="Ticket not found",
        )

    booking = db.query(Booking).filter(
        Booking.id == ticket.booking_id
    ).first()

    if not booking or booking.booking_number != booking_number:
        raise HTTPException(
            status_code=400,
            detail="QR booking mismatch",
        )

    check_confirmed_and_paid(booking, db)

    show, event, screen, venue = get_show_and_event(
        db,
        booking.show_id,
    )

    if venue.id != current_user.assigned_venue_id:
        raise HTTPException(
            status_code=403,
            detail="This show is not at your assigned venue",
        )

    now = datetime.utcnow()

    if now < show.start_time - timedelta(hours=1):
        raise HTTPException(
            status_code=400,
            detail="Ticket scanning opens 1 hour before the show",
        )

    if now > show.end_time:
        raise HTTPException(
            status_code=400,
            detail="The show has ended; ticket scanning is closed",
        )

    if ticket.scanned_at is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Ticket already used at {ticket.scanned_at}",
        )

    ticket = (
        db.query(BookingSeat)
        .filter(BookingSeat.id == ticket.id)
        .with_for_update()
        .first()
    )

    if ticket.scanned_at is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Ticket already used at {ticket.scanned_at}",
        )

    ticket.scanned_at = now
    db.commit()

    all_tickets = (
        db.query(BookingSeat)
        .filter(BookingSeat.booking_id == booking.id)
        .all()
    )

    seat_labels = []

    for booked_seat in all_tickets:
        seat = db.query(Seat).filter(
            Seat.id == booked_seat.seat_id
        ).first()

        if seat:
            seat_labels.append(
                f"{seat.row_label}{seat.seat_number}"
            )

    return {
        "message": "Ticket scanned successfully",
        "customer_name": booking.customer.full_name,
        "booking_number": booking.booking_number,
        "seats": seat_labels,
        "seat_count": booking.seat_count,
        "event": event.title,
        "venue": venue.name,
        "scanned_at": ticket.scanned_at,
    }


@router.get("/shows/{show_id}/entries")
def get_show_entries(
    show_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    show, event, screen, venue = get_show_and_event(
        db,
        show_id,
    )

    if current_user.role == "Staff":
        if current_user.assigned_venue_id != venue.id:
            raise HTTPException(
                status_code=403,
                detail="Access denied",
            )

    elif current_user.role == "Organizer":
        if event.organizer_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="Access denied",
            )

    elif current_user.role != "Admin":
        raise HTTPException(
            status_code=403,
            detail="Access denied",
        )

    total_bookings = (
        db.query(func.count(Booking.id))
        .filter(
            Booking.show_id == show_id,
            Booking.status == "Confirmed",
        )
        .scalar()
        or 0
    )

    entered_bookings = (
        db.query(func.count(func.distinct(BookingSeat.booking_id)))
        .join(
            Booking,
            Booking.id == BookingSeat.booking_id,
        )
        .filter(
            Booking.show_id == show_id,
            Booking.status == "Confirmed",
            BookingSeat.scanned_at.isnot(None),
        )
        .scalar()
        or 0
    )

    return {
        "show_id": show_id,
        "event": event.title,
        "venue": venue.name,
        "entered_bookings": entered_bookings,
        "total_confirmed_bookings": total_bookings,
        "remaining_bookings": max(
            total_bookings - entered_bookings,
            0,
        ),
    }


