from datetime import datetime, timedelta
import inspect
import logging

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth.dependencies import get_current_user
from app.models.models import (
    User,
    Show,
    Event,
    Booking,
    BookingSeat,
    Payment,
    Refund,
    WaitlistEntry,
    Notification,
    Seat,
)
from app.redis_client import redis_client
from app.routers.seat_websocket import publish_seat_updates
from app.utils.notification_tasks import queue_email

router = APIRouter(tags=["Cancellations and Waitlist"])
logger = logging.getLogger(__name__)


class WaitlistCreate(BaseModel):
    seat_count: int = Field(default=1, ge=1, le=10)
    preferred_category: str = Field(min_length=1, max_length=30)


def current_time():
    return datetime.now()


def require_role(user: User, *roles):
    if user.role not in roles:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to perform this action",
        )


def get_show_event(db: Session, show_id: int):
    show = db.query(Show).filter(Show.id == show_id).first()

    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    event = db.query(Event).filter(Event.id == show.event_id).first()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    return show, event


def get_booking_or_404(db: Session, booking_id: int):
    booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .first()
    )

    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    return booking


def authorize_booking(booking: Booking, user: User):
    if user.role == "Admin":
        return

    if booking.customer_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You cannot access another customer's booking",
        )


def add_notification(
    db: Session,
    user_id: int,
    title: str,
    message: str,
):
    notification = Notification(
        user_id=user_id,
        title=title,
        message=message,
        is_read=False,
    )
    db.add(notification)


def get_successful_payment(db: Session, booking_id: int):
    return (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking_id,
            Payment.status == "Success",
        )
        .order_by(Payment.id.desc())
        .first()
    )


def create_refund(
    db: Session,
    booking: Booking,
    amount,
    reason: str,
    payment: Payment | None,
):
    existing_refund = (
        db.query(Refund)
        .filter(
            Refund.booking_id == booking.id,
            Refund.status == "Success",
        )
        .first()
    )

    if existing_refund:
        return existing_refund

    refund = Refund(
        booking_id=booking.id,
        payment_id=payment.id if payment else None,
        amount=amount,
        reason=reason,
        status="Success",
        refunded_at=current_time(),
    )

    db.add(refund)

    if payment:
        payment.status = "Refunded"

    return refund


def release_redis_locks(booking: Booking, seat_ids: list[int]):
    """Delete Redis locks only when they belong to this customer."""
    for seat_id in seat_ids:
        key = f"lock:show:{booking.show_id}:seat:{seat_id}"

        try:
            owner = redis_client.get(key)

            if owner is None:
                continue

            if isinstance(owner, bytes):
                owner = owner.decode("utf-8")

            if str(owner) == str(booking.customer_id):
                redis_client.delete(key)

        except Exception:
            logger.exception(
                "Could not release Redis lock for show %s, seat %s",
                booking.show_id,
                seat_id,
            )


async def publish_seat_map(show_id: int, seat_ids: list[int]):
    """Broadcast released seats without undoing the database action."""
    if not seat_ids:
        return

    try:
        result = publish_seat_updates(
            show_id,
            seat_ids,
            "Available",
        )

        if inspect.isawaitable(result):
            await result

    except Exception:
        logger.exception(
            "Could not broadcast released seats for show %s",
            show_id,
        )


def queue_user_email(
    background_tasks: BackgroundTasks,
    user: User,
    subject: str,
    message: str,
):
    """Queue an email without making SMTP failure undo a booking action."""
    if not user.email:
        return

    try:
        queue_email(
            background_tasks,
            user.email,
            subject,
            message,
        )
    except Exception:
        logger.exception(
            "Could not queue email to customer ID %s",
            user.id,
        )


def queue_cancellation_email(
    background_tasks: BackgroundTasks,
    customer: User,
    booking: Booking,
    event: Event,
    refund_amount,
):
    queue_user_email(
        background_tasks,
        customer,
        "Booking cancellation and refund confirmation",
        (
            f"Hello {customer.full_name},\n\n"
            f"Your booking {booking.booking_number} for "
            f"'{event.title}' has been cancelled.\n\n"
            f"Refund amount: {refund_amount}\n"
            "Refund reason: Customer Cancellation\n\n"
            "Please note that this application records a mock refund; "
            "no real payment has been transferred.\n\n"
            "Thank you."
        ),
    )


def queue_show_cancellation_email(
    background_tasks: BackgroundTasks,
    customer: User,
    booking: Booking,
    event: Event,
):
    queue_user_email(
        background_tasks,
        customer,
        "Show cancellation and full refund confirmation",
        (
            f"Hello {customer.full_name},\n\n"
            f"The show for '{event.title}' has been cancelled.\n"
            f"Booking number: {booking.booking_number}\n"
            f"Full mock refund amount: {booking.grand_total}\n\n"
            "The recorded refund includes ticket charges, fees, and GST.\n"
            "This is a mock refund record; no real payment has been transferred.\n\n"
            "We apologize for the inconvenience."
        ),
    )


def queue_waitlist_email(
    background_tasks: BackgroundTasks,
    customer: User,
    event: Event,
    show: Show,
    category: str,
):
    queue_user_email(
        background_tasks,
        customer,
        "Waitlist seat availability update",
        (
            f"Hello {customer.full_name},\n\n"
            f"Seats in your preferred category '{category}' may now "
            f"be available for '{event.title}'.\n"
            f"Show time: {show.start_time.strftime('%Y-%m-%d %H:%M')}\n\n"
            "Please open the booking page to check current availability. "
            "Seats are not reserved by this email and may be booked by others.\n\n"
            "Thank you."
        ),
    )


def get_seat_rows(db: Session, booking_id: int):
    return (
        db.query(BookingSeat)
        .filter(BookingSeat.booking_id == booking_id)
        .all()
    )


def release_booking_seats(db: Session, booking: Booking):
    """Capture seat IDs before removing the booking-seat associations."""
    rows = get_seat_rows(db, booking.id)
    seat_ids = [row.seat_id for row in rows]

    for row in rows:
        db.delete(row)

    return seat_ids


def notify_waitlist_for_released_seats(
    db: Session,
    show_id: int,
    released_seat_ids: list[int],
    background_tasks: BackgroundTasks,
):
    """Notify matching waitlist customers in queue order."""
    if not released_seat_ids:
        return

    show, event = get_show_event(db, show_id)

    released_seats = (
        db.query(Seat)
        .filter(Seat.id.in_(released_seat_ids))
        .all()
    )

    available_categories = {seat.category for seat in released_seats}

    if not available_categories:
        return

    waiting_entries = (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.show_id == show_id,
            WaitlistEntry.status == "Waiting",
        )
        .order_by(WaitlistEntry.id.asc())
        .all()
    )

    # Notify at most one waiting customer per category in this pass.
    # This avoids sending the same availability alert to everyone.
    notified_categories = set()

    for entry in waiting_entries:
        category = entry.preferred_category

        if category not in available_categories:
            continue

        if category in notified_categories:
            continue

        customer = (
            db.query(User)
            .filter(User.id == entry.customer_id)
            .first()
        )

        if not customer:
            continue

        entry.status = "Notified"
        notified_categories.add(category)

        message = (
            f"Seats in your preferred category '{category}' may now be "
            f"available for '{event.title}'. Please check the booking page."
        )

        add_notification(
            db,
            customer.id,
            "Waitlist seats available",
            message,
        )

        queue_waitlist_email(
            background_tasks,
            customer,
            event,
            show,
            category,
        )


def customer_refund_amount(booking: Booking, show: Show):
    """
    Refund rules:
    More than 24 hours before show: 100% of ticket_total.
    From 2 to 24 hours before show: 50% of ticket_total.
    Less than 2 hours before show: cancellation not allowed.

    Convenience fee and GST are not refunded for customer cancellation.
    """
    remaining = show.start_time - current_time()

    if remaining < timedelta(hours=2):
        raise HTTPException(
            status_code=400,
            detail="Cancellation is not allowed within 2 hours of the show",
        )

    if remaining > timedelta(hours=24):
        return booking.ticket_total

    return booking.ticket_total * 0.50


# -------------------- CUSTOMER CANCELLATION --------------------

@router.put("/bookings/{booking_id}/cancel")
async def cancel_booking(
    booking_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Customer", "Admin")

    booking = get_booking_or_404(db, booking_id)
    authorize_booking(booking, current_user)

    if booking.status != "Confirmed":
        raise HTTPException(
            status_code=400,
            detail="Only confirmed bookings can be cancelled",
        )

    show, event = get_show_event(db, booking.show_id)

    refund_amount = customer_refund_amount(booking, show)
    payment = get_successful_payment(db, booking.id)

    seat_ids = release_booking_seats(db, booking)

    create_refund(
        db=db,
        booking=booking,
        amount=refund_amount,
        reason="Customer Cancellation",
        payment=payment,
    )

    booking.status = "Cancelled"

    add_notification(
        db,
        booking.customer_id,
        "Booking cancelled",
        f"Booking {booking.booking_number} was cancelled. "
        f"Refund amount: {refund_amount}.",
    )

    customer = (
        db.query(User)
        .filter(User.id == booking.customer_id)
        .first()
    )

    if customer:
        queue_cancellation_email(
            background_tasks,
            customer,
            booking,
            event,
            refund_amount,
        )

    notify_waitlist_for_released_seats(
        db,
        booking.show_id,
        seat_ids,
        background_tasks,
    )

    db.commit()

    release_redis_locks(booking, seat_ids)
    await publish_seat_map(booking.show_id, seat_ids)

    return {
        "message": "Booking cancelled successfully",
        "booking_id": booking.id,
        "booking_number": booking.booking_number,
        "refund_amount": float(refund_amount),
        "refund_reason": "Customer Cancellation",
        "released_seat_ids": seat_ids,
    }


@router.get("/bookings/{booking_id}/refunds")
def get_booking_refunds(
    booking_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Customer", "Admin")

    booking = get_booking_or_404(db, booking_id)
    authorize_booking(booking, current_user)

    refunds = (
        db.query(Refund)
        .filter(Refund.booking_id == booking_id)
        .order_by(Refund.id.desc())
        .all()
    )

    return [
        {
            "refund_id": refund.id,
            "booking_id": refund.booking_id,
            "payment_id": refund.payment_id,
            "amount": float(refund.amount),
            "reason": refund.reason,
            "status": refund.status,
            "refunded_at": refund.refunded_at,
        }
        for refund in refunds
    ]


# -------------------- ORGANIZER / ADMIN SHOW CANCELLATION --------------------

@router.put("/shows/{show_id}/cancel")
async def cancel_show(
    show_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Organizer", "Admin")

    show, event = get_show_event(db, show_id)

    if current_user.role == "Organizer":
        if event.organizer_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="You can cancel only your own event's shows",
            )

    if show.status == "Cancelled":
        raise HTTPException(
            status_code=400,
            detail="This show is already cancelled",
        )

    show.status = "Cancelled"

    confirmed_bookings = (
        db.query(Booking)
        .filter(
            Booking.show_id == show_id,
            Booking.status == "Confirmed",
        )
        .all()
    )

    pending_bookings = (
        db.query(Booking)
        .filter(
            Booking.show_id == show_id,
            Booking.status == "Pending Payment",
        )
        .all()
    )

    all_released_seat_ids = []
    released_seat_ids_by_booking = {}

    # Confirmed bookings receive a full mock refund.
    for booking in confirmed_bookings:
        payment = get_successful_payment(db, booking.id)

        create_refund(
            db=db,
            booking=booking,
            amount=booking.grand_total,
            reason="Show Cancellation",
            payment=payment,
        )

        booking.status = "Cancelled"

        released_ids = release_booking_seats(db, booking)
        released_seat_ids_by_booking[booking.id] = released_ids
        all_released_seat_ids.extend(released_ids)

        add_notification(
            db,
            booking.customer_id,
            "Show cancelled",
            f"The show '{event.title}' has been cancelled. "
            f"Your full refund is {booking.grand_total}.",
        )

        customer = (
            db.query(User)
            .filter(User.id == booking.customer_id)
            .first()
        )

        if customer:
            queue_show_cancellation_email(
                background_tasks,
                customer,
                booking,
                event,
            )

    # Pending bookings have not completed payment.
    for booking in pending_bookings:
        booking.status = "Cancelled"

        released_ids = release_booking_seats(db, booking)
        released_seat_ids_by_booking[booking.id] = released_ids
        all_released_seat_ids.extend(released_ids)

        add_notification(
            db,
            booking.customer_id,
            "Show cancelled",
            f"The show '{event.title}' has been cancelled.",
        )

        customer = (
            db.query(User)
            .filter(User.id == booking.customer_id)
            .first()
        )

        if customer:
            queue_user_email(
                background_tasks,
                customer,
                "Show cancellation",
                (
                    f"The show '{event.title}' has been cancelled.\n"
                    f"Your pending booking {booking.booking_number} "
                    "has also been cancelled. No refund is due because "
                    "the payment was not completed."
                ),
            )

    # Cancel waitlist entries for the cancelled show.
    waiting_entries = (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.show_id == show_id,
            WaitlistEntry.status == "Waiting",
        )
        .all()
    )

    for entry in waiting_entries:
        entry.status = "Cancelled"

        customer = (
            db.query(User)
            .filter(User.id == entry.customer_id)
            .first()
        )

        if customer:
            queue_user_email(
                background_tasks,
                customer,
                "Waitlist closed: show cancelled",
                (
                    f"The show '{event.title}' has been cancelled. "
                    "Your waitlist entry has been closed."
                ),
            )

    db.commit()

    for booking in confirmed_bookings + pending_bookings:
        seat_ids = released_seat_ids_by_booking.get(booking.id, [])
        release_redis_locks(booking, seat_ids)

    await publish_seat_map(show_id, all_released_seat_ids)

    return {
        "message": "Show cancelled successfully",
        "show_id": show_id,
        "event_title": event.title,
        "cancelled_confirmed_bookings": len(confirmed_bookings),
        "cancelled_pending_bookings": len(pending_bookings),
        "full_refunds_created": len(confirmed_bookings),
        "refund_reason": "Show Cancellation",
        "released_seat_ids": all_released_seat_ids,
    }


# -------------------- WAITLIST --------------------

@router.post("/shows/{show_id}/waitlist")
def join_waitlist(
    show_id: int,
    payload: WaitlistCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Customer")

    show, event = get_show_event(db, show_id)

    if show.status != "Scheduled":
        raise HTTPException(
            status_code=400,
            detail="You can join the waitlist only for a scheduled show",
        )

    existing_entry = (
        db.query(WaitlistEntry)
        .filter(
            WaitlistEntry.customer_id == current_user.id,
            WaitlistEntry.show_id == show_id,
        )
        .first()
    )

    if existing_entry:
        raise HTTPException(
            status_code=409,
            detail="You have already joined this show's waitlist",
        )

    existing_booking = (
        db.query(Booking)
        .filter(
            Booking.customer_id == current_user.id,
            Booking.show_id == show_id,
            Booking.status.in_(["Confirmed", "Pending Payment"]),
        )
        .first()
    )

    if existing_booking:
        raise HTTPException(
            status_code=400,
            detail="You already have an active booking for this show",
        )

    category_exists = (
        db.query(Seat)
        .filter(
            Seat.screen_id == show.screen_id,
            Seat.category == payload.preferred_category,
            Seat.is_active.is_(True),
        )
        .first()
    )

    if not category_exists:
        raise HTTPException(
            status_code=400,
            detail="Invalid seat category for this show's screen",
        )

    total_category_seats = (
        db.query(Seat)
        .filter(
            Seat.screen_id == show.screen_id,
            Seat.category == payload.preferred_category,
            Seat.is_active.is_(True),
        )
        .count()
    )

    booked_category_seats = (
        db.query(BookingSeat)
        .join(Seat, BookingSeat.seat_id == Seat.id)
        .join(Booking, BookingSeat.booking_id == Booking.id)
        .filter(
            BookingSeat.show_id == show_id,
            Seat.category == payload.preferred_category,
            Booking.status.in_(["Confirmed", "Pending Payment"]),
        )
        .count()
    )

    if booked_category_seats < total_category_seats:
        raise HTTPException(
            status_code=400,
            detail=(
                "Seats are still available in your preferred category. "
                "Please book directly instead of joining the waitlist."
            ),
        )

    entry = WaitlistEntry(
        customer_id=current_user.id,
        show_id=show_id,
        seat_count=payload.seat_count,
        preferred_category=payload.preferred_category,
        status="Waiting",
    )

    db.add(entry)
    db.commit()
    db.refresh(entry)

    return {
        "message": "Added to waitlist successfully",
        "waitlist_id": entry.id,
        "show_id": entry.show_id,
        "event_title": event.title,
        "seat_count": entry.seat_count,
        "preferred_category": entry.preferred_category,
        "status": entry.status,
    }


@router.get("/shows/{show_id}/waitlist")
def get_show_waitlist(
    show_id: int,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    show, event = get_show_event(db, show_id)

    if current_user.role == "Customer":
        query = db.query(WaitlistEntry).filter(
            WaitlistEntry.show_id == show_id,
            WaitlistEntry.customer_id == current_user.id
        )

    elif current_user.role == "Organizer":
        if event.organizer_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="You can view waitlists only for your own events"
            )

        query = db.query(WaitlistEntry).filter(
            WaitlistEntry.show_id == show_id
        )

    elif current_user.role == "Admin":
        query = db.query(WaitlistEntry).filter(
            WaitlistEntry.show_id == show_id
        )

    else:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to view this waitlist"
        )

    entries = (
        query
        .order_by(WaitlistEntry.id.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return [
        {
            "waitlist_id": entry.id,
            "customer_id": entry.customer_id,
            "show_id": entry.show_id,
            "seat_count": entry.seat_count,
            "preferred_category": entry.preferred_category,
            "status": entry.status,
            "position": (
                skip + index + 1
                if entry.status == "Waiting"
                else None
            ),
        }
        for index, entry in enumerate(entries)
    ]


@router.delete("/waitlist/{waitlist_id}")
def leave_waitlist(
    waitlist_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    entry = (
        db.query(WaitlistEntry)
        .filter(WaitlistEntry.id == waitlist_id)
        .first()
    )

    if not entry:
        raise HTTPException(
            status_code=404,
            detail="Waitlist entry not found",
        )

    if (
        current_user.role != "Admin"
        and entry.customer_id != current_user.id
    ):
        raise HTTPException(
            status_code=403,
            detail="You cannot remove another customer's waitlist entry",
        )

    db.delete(entry)
    db.commit()

    return {
        "message": "Removed from waitlist successfully",
        "waitlist_id": waitlist_id,
    }

