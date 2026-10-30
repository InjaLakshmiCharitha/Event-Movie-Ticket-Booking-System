import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func

from app.database import SessionLocal
from app.models.models import (
    Booking,
    BookingSeat,
    CouponUsage,
    Event,
    Show,
)
from app.redis_client import redis_client
from app.routers.seat_lock import lock_key
from app.routers.seat_websocket import publish_seat_updates
from app.utils.email_service import send_email

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# Delete a Redis lock only when it still belongs to
# the customer whose pending booking has expired.
RELEASE_EXPIRED_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def expire_pending_bookings():
    """
    Expire unpaid bookings after expires_at.
    Remove their temporary seat records and coupon usage,
    release only their own Redis locks, and broadcast seat updates.
    """
    db = SessionLocal()

    try:
        now = datetime.now()

        bookings = (
            db.query(Booking)
            .filter(
                Booking.status == "Pending Payment",
                Booking.expires_at <= now,
            )
            .all()
        )

        updates = []

        for booking in bookings:
            seat_rows = (
                db.query(BookingSeat)
                .filter(BookingSeat.booking_id == booking.id)
                .all()
            )

            seat_ids = [row.seat_id for row in seat_rows]

            # Release each lock only if the Redis value still matches
            # this booking's customer ID.
            for seat_id in seat_ids:
                try:
                    redis_client.eval(
                        RELEASE_EXPIRED_LOCK_SCRIPT,
                        1,
                        lock_key(booking.show_id, seat_id),
                        str(booking.customer_id),
                    )
                except Exception:
                    logger.exception(
                        "Could not safely release Redis lock "
                        "for show %s, seat %s",
                        booking.show_id,
                        seat_id,
                    )

            booking.status = "Expired"

            db.query(BookingSeat).filter(
                BookingSeat.booking_id == booking.id
            ).delete(synchronize_session=False)

            db.query(CouponUsage).filter(
                CouponUsage.booking_id == booking.id
            ).delete(synchronize_session=False)

            if seat_ids:
                updates.append((booking.show_id, seat_ids))

        # Commit database changes before broadcasting.
        db.commit()

        # Broadcast each affected seat's current Redis lock state.
        for show_id, seat_ids in updates:
            for seat_id in seat_ids:
                try:
                    owner = redis_client.get(
                        lock_key(show_id, seat_id)
                    )

                    status = "Locked" if owner else "Available"

                    asyncio.run(
                        publish_seat_updates(
                            show_id,
                            [seat_id],
                            status,
                        )
                    )

                except Exception:
                    logger.exception(
                        "Could not broadcast expired seat update "
                        "for show %s, seat %s",
                        show_id,
                        seat_id,
                    )

        if bookings:
            logger.info(
                "Expired %s pending booking(s)",
                len(bookings),
            )

    except Exception:
        db.rollback()
        logger.exception("Pending-booking expiry job failed")

    finally:
        db.close()


def send_upcoming_show_reminders():
    """
    Email customers with confirmed bookings for shows starting
    within the next three hours. Each booking gets one reminder
    after successful email delivery.
    """
    db = SessionLocal()

    try:
        now = datetime.now()
        reminder_window = now + timedelta(hours=3)

        bookings = (
            db.query(Booking)
            .join(Show, Booking.show_id == Show.id)
            .filter(
                Booking.status == "Confirmed",
                Booking.reminder_sent.is_(False),
                Show.status == "Scheduled",
                Show.start_time > now,
                Show.start_time <= reminder_window,
            )
            .all()
        )

        for booking in bookings:
            try:
                customer = booking.customer
                show = booking.show
                event = show.event

                subject = f"Reminder: {event.title} is coming up"

                body = (
                    f"Hello {customer.full_name},\n\n"
                    "This is a reminder for your confirmed booking.\n\n"
                    f"Event: {event.title}\n"
                    f"Booking number: {booking.booking_number}\n"
                    f"Show starts: "
                    f"{show.start_time.strftime('%Y-%m-%d %H:%M')}\n"
                    f"Number of seats: {booking.seat_count}\n\n"
                    "Please arrive at the venue in time and keep "
                    "your ticket QR code ready.\n\n"
                    "Thank you for booking with us."
                )

                sent = send_email(
                    customer.email,
                    subject,
                    body,
                )

                if sent:
                    booking.reminder_sent = True

            except Exception:
                logger.exception(
                    "Reminder failed for booking %s",
                    booking.booking_number,
                )

        db.commit()

    except Exception:
        db.rollback()
        logger.exception("Upcoming-show reminder job failed")

    finally:
        db.close()


def mark_completed_shows():
    """Mark scheduled shows Completed once their end time passes."""
    db = SessionLocal()

    try:
        now = datetime.now()

        result = (
            db.query(Show)
            .filter(
                Show.status == "Scheduled",
                Show.end_time <= now,
            )
            .update(
                {"status": "Completed"},
                synchronize_session=False,
            )
        )

        db.commit()

        if result:
            logger.info("Marked %s show(s) as Completed", result)

    except Exception:
        db.rollback()
        logger.exception("Show completion job failed")

    finally:
        db.close()


def mark_completed_events():
    """
    Mark published events Completed when they have at least one
    show and all their shows have ended.
    """
    db = SessionLocal()

    try:
        now = datetime.now()

        events = (
            db.query(Event)
            .filter(Event.status == "Published")
            .all()
        )

        completed_count = 0

        for event in events:
            show_count = (
                db.query(func.count(Show.id))
                .filter(Show.event_id == event.id)
                .scalar()
            ) or 0

            unfinished_count = (
                db.query(func.count(Show.id))
                .filter(
                    Show.event_id == event.id,
                    Show.end_time > now,
                )
                .scalar()
            ) or 0

            if show_count > 0 and unfinished_count == 0:
                event.status = "Completed"
                completed_count += 1

        db.commit()

        if completed_count:
            logger.info(
                "Marked %s event(s) as Completed",
                completed_count,
            )

    except Exception:
        db.rollback()
        logger.exception("Event completion job failed")

    finally:
        db.close()


def start_scheduler():
    """Register and start all Level 11 scheduled jobs."""
    if scheduler.running:
        return

    scheduler.add_job(
        expire_pending_bookings,
        trigger="interval",
        minutes=1,
        id="expire_pending_bookings",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        send_upcoming_show_reminders,
        trigger="interval",
        minutes=30,
        id="upcoming_show_reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        mark_completed_shows,
        trigger="interval",
        minutes=15,
        id="mark_completed_shows",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        mark_completed_events,
        trigger="cron",
        hour=0,
        minute=10,
        id="mark_completed_events",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info("Event booking scheduler started")


def stop_scheduler():
    """Stop the scheduler when the application shuts down."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Event booking scheduler stopped")
