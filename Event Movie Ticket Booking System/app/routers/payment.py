import os
import hmac
import hashlib
import logging
import secrets
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import razorpay

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
)
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import (
    User,
    Booking,
    BookingSeat,
    Payment,
    Notification,
)
from app.redis_client import redis_client
from app.routers.seat_websocket import publish_seat_updates
from app.routers.ticket import generate_booking_ticket_pdf
from app.schemas.booking import (
    MockPaymentRequest,
    PaymentResponse,
    RazorpayOrderResponse,
    RazorpayVerifyRequest,
)
from app.auth.dependencies import require_roles
from app.utils.notification_tasks import (
    send_booking_confirmation_email,
    send_payment_failed_email,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/bookings",
    tags=["Payments"],
)

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")


def get_razorpay_client():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Razorpay configuration is missing",
        )

    return razorpay.Client(
        auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
    )


def lock_key(show_id: int, seat_id: int) -> str:
    return f"seat_lock:{show_id}:{seat_id}"


def get_accessible_booking(
    booking_id: int,
    current_user: User,
    db: Session,
) -> Booking:
    booking = (
        db.query(Booking)
        .filter(Booking.id == booking_id)
        .first()
    )

    if not booking:
        raise HTTPException(404, "Booking not found")

    if current_user.role == "Admin":
        return booking

    if current_user.role == "Organizer":
        if booking.show.event.organizer_id != current_user.id:
            raise HTTPException(403, "You cannot access this booking")
        return booking

    if current_user.role == "Customer":
        if booking.customer_id != current_user.id:
            raise HTTPException(403, "You cannot access this booking")
        return booking

    raise HTTPException(403, "Permission denied")


def notify_customer(
    db: Session,
    customer_id: int,
    message: str,
    title: str = "Booking Notification",
):
    db.add(
        Notification(
            user_id=customer_id,
            title=title,
            message=message,
            is_read=False,
        )
    )


def get_booking_seat_ids(
    db: Session,
    booking_id: int,
) -> list[int]:
    rows = (
        db.query(BookingSeat)
        .filter(BookingSeat.booking_id == booking_id)
        .all()
    )
    return [row.seat_id for row in rows]


def release_locks(
    show_id: int,
    seat_ids: list[int],
    customer_id: int,
):
    script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    end
    return 0
    """

    for seat_id in seat_ids:
        try:
            redis_client.eval(
                script,
                1,
                lock_key(show_id, seat_id),
                str(customer_id),
            )
        except Exception:
            logger.exception(
                "Could not release seat lock: %s",
                seat_id,
            )


def verify_seat_locks(
    show_id: int,
    seat_ids: list[int],
    customer_id: int,
):
    for seat_id in seat_ids:
        key = lock_key(show_id, seat_id)

        try:
            owner = redis_client.get(key)
            ttl = redis_client.ttl(key)
        except Exception:
            logger.exception("Redis lock verification failed")
            raise HTTPException(
                status_code=503,
                detail="Seat-lock service unavailable",
            )

        if (
            owner is None
            or str(owner) != str(customer_id)
            or ttl <= 0
        ):
            raise HTTPException(
                status_code=409,
                detail="Your seat lock has expired. Please book again.",
            )


def confirm_successful_payment(
    db: Session,
    payment: Payment,
    background_tasks: BackgroundTasks | None = None,
):
    """
    Confirm a booking only once after a verified payment.
    Returns False if the payment was already processed.
    """

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment.id)
        .with_for_update()
        .first()
    )

    if not payment:
        return False

    if payment.status == "Success":
        return False

    booking = (
        db.query(Booking)
        .filter(Booking.id == payment.booking_id)
        .with_for_update()
        .first()
    )

    if not booking:
        raise HTTPException(404, "Booking not found")

    if booking.status != "Pending Payment":
        payment.status = "Review Required"
        notify_customer(
            db,
            booking.customer_id,
            "Payment was received, but the booking is no longer "
            "pending. Please contact support.",
            "Payment Requires Review",
        )
        db.commit()
        return False

    if booking.expires_at and booking.expires_at <= datetime.now():
        booking.status = "Expired"
        payment.status = "Review Required"

        notify_customer(
            db,
            booking.customer_id,
            "Payment was received after your booking expired. "
            "Please contact support for assistance.",
            "Payment Requires Review",
        )
        db.commit()
        return False

    seat_ids = get_booking_seat_ids(db, booking.id)

    if not seat_ids:
        payment.status = "Review Required"
        db.commit()
        return False

    competing_booking = (
        db.query(BookingSeat)
        .join(Booking, Booking.id == BookingSeat.booking_id)
        .filter(
            Booking.show_id == booking.show_id,
            BookingSeat.seat_id.in_(seat_ids),
            Booking.id != booking.id,
            Booking.status.in_(["Pending Payment", "Confirmed"]),
        )
        .first()
    )

    if competing_booking:
        payment.status = "Review Required"
        booking.status = "Cancelled"

        notify_customer(
            db,
            booking.customer_id,
            "Payment was received but your seats are no longer "
            "available. Please contact support for a refund.",
            "Payment Requires Review",
        )
        db.commit()
        return False

    payment.status = "Success"
    payment.paid_at = datetime.now()

    if payment.razorpay_payment_id:
        payment.transaction_id = payment.razorpay_payment_id

    booking.status = "Confirmed"
    booking.confirmed_at = datetime.now()

    notify_customer(
        db,
        booking.customer_id,
        f"Your booking {booking.booking_number} is confirmed.",
        "Booking Confirmed",
    )

    db.commit()
    db.refresh(booking)

    release_locks(
        booking.show_id,
        seat_ids,
        booking.customer_id,
    )

    return True


def send_ticket_notifications(
    booking: Booking,
    db: Session,
    background_tasks: BackgroundTasks | None,
):
    if background_tasks is None:
        return

    try:
        pdf_path = generate_booking_ticket_pdf(booking, db)

        send_booking_confirmation_email(
            background_tasks=background_tasks,
            recipient=booking.customer.email,
            booking_number=booking.booking_number,
            event_title=booking.show.event.title,
            show_time=booking.show.start_time.strftime(
                "%Y-%m-%d %H:%M"
            ),
            ticket_pdf_path=pdf_path,
        )
    except Exception:
        logger.exception(
            "Ticket email/PDF failed for booking %s",
            booking.booking_number,
        )


async def broadcast_booking_seats(
    show_id: int,
    seat_ids: list[int],
):
    try:
        await publish_seat_updates(
            show_id,
            seat_ids,
            "Booked",
        )
    except Exception:
        logger.exception(
            "Could not publish seat updates for show %s",
            show_id,
        )


# ---------------------------------------------------------
# EXISTING MOCK PAYMENT
# ---------------------------------------------------------

@router.post("/{booking_id}/pay")
async def process_mock_payment(
    booking_id: int,
    payment_data: MockPaymentRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Customer")),
):
    booking = (
        db.query(Booking)
        .filter(
            Booking.id == booking_id,
            Booking.customer_id == current_user.id,
        )
        .first()
    )

    if not booking:
        raise HTTPException(404, "Booking not found")

    if booking.status != "Pending Payment":
        raise HTTPException(
            409,
            f"Cannot pay for booking with status {booking.status}",
        )

    now = datetime.now()

    if booking.expires_at and booking.expires_at <= now:
        booking.status = "Expired"
        db.commit()
        raise HTTPException(409, "Booking has expired")

    seat_ids = get_booking_seat_ids(db, booking.id)

    if not seat_ids:
        raise HTTPException(409, "No seats found for this booking")

    if payment_data.simulate_status == "Failed":
        payment = Payment(
            booking_id=booking.id,
            amount=booking.grand_total,
            payment_method=payment_data.payment_method,
            status="Failed",
            transaction_id=f"MOCK-{secrets.token_hex(6).upper()}",
        )
        db.add(payment)

        notify_customer(
            db,
            booking.customer_id,
            f"Payment failed for booking {booking.booking_number}.",
            "Payment Failed",
        )
        db.commit()

        try:
            send_payment_failed_email(
                background_tasks=background_tasks,
                recipient=current_user.email,
                booking_number=booking.booking_number,
            )
        except Exception:
            logger.exception("Could not queue failure email")

        return {
            "message": "Payment failed",
            "booking_id": booking.id,
            "booking_number": booking.booking_number,
            "payment_status": "Failed",
            "booking_status": booking.status,
        }

    competing_booking = (
        db.query(BookingSeat)
        .join(Booking, Booking.id == BookingSeat.booking_id)
        .filter(
            Booking.show_id == booking.show_id,
            BookingSeat.seat_id.in_(seat_ids),
            Booking.id != booking.id,
            Booking.status.in_(["Pending Payment", "Confirmed"]),
        )
        .first()
    )

    if competing_booking:
        raise HTTPException(
            409,
            "One or more seats are no longer available",
        )

    verify_seat_locks(
        booking.show_id,
        seat_ids,
        current_user.id,
    )

    payment = Payment(
        booking_id=booking.id,
        amount=booking.grand_total,
        payment_method=payment_data.payment_method,
        status="Success",
        transaction_id=f"MOCK-{secrets.token_hex(6).upper()}",
        paid_at=now,
    )
    db.add(payment)

    booking.status = "Confirmed"
    booking.confirmed_at = now

    notify_customer(
        db,
        booking.customer_id,
        f"Your booking {booking.booking_number} is confirmed.",
        "Booking Confirmed",
    )

    try:
        db.commit()
        db.refresh(booking)
    except Exception:
        db.rollback()
        logger.exception("Mock payment database commit failed")
        raise HTTPException(500, "Could not complete payment")

    release_locks(
        booking.show_id,
        seat_ids,
        current_user.id,
    )

    await broadcast_booking_seats(
        booking.show_id,
        seat_ids,
    )

    send_ticket_notifications(
        booking,
        db,
        background_tasks,
    )

    return {
        "message": "Payment successful",
        "booking_id": booking.id,
        "booking_number": booking.booking_number,
        "payment_status": "Success",
        "booking_status": booking.status,
    }


# ---------------------------------------------------------
# RAZORPAY ORDER CREATION
# ---------------------------------------------------------

@router.post(
    "/{booking_id}/create-order",
    response_model=RazorpayOrderResponse,
)
def create_razorpay_order(
    booking_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Customer")),
):
    booking = (
        db.query(Booking)
        .filter(
            Booking.id == booking_id,
            Booking.customer_id == current_user.id,
        )
        .first()
    )

    if not booking:
        raise HTTPException(404, "Booking not found")

    if booking.status != "Pending Payment":
        raise HTTPException(
            409,
            "Booking is not awaiting payment",
        )

    if booking.expires_at and booking.expires_at <= datetime.now():
        booking.status = "Expired"
        db.commit()
        raise HTTPException(409, "Booking has expired")

    seat_ids = get_booking_seat_ids(db, booking.id)

    if not seat_ids:
        raise HTTPException(409, "No seats found")

    verify_seat_locks(
        booking.show_id,
        seat_ids,
        current_user.id,
    )

    # Reuse an existing active Razorpay order for this booking.
    existing_payment = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking.id,
            Payment.payment_method == "Razorpay",
            Payment.status == "Created",
            Payment.razorpay_order_id.isnot(None),
        )
        .order_by(Payment.id.desc())
        .first()
    )

    if existing_payment:
        amount_paise = int(
            (Decimal(str(existing_payment.amount)) * 100)
            .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )

        return {
            "booking_id": booking.id,
            "razorpay_order_id": existing_payment.razorpay_order_id,
            "amount": amount_paise,
            "currency": "INR",
            "key_id": RAZORPAY_KEY_ID,
        }

    amount_paise = int(
        (Decimal(str(booking.grand_total)) * 100)
        .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    if amount_paise <= 0:
        raise HTTPException(400, "Invalid payment amount")

    client = get_razorpay_client()

    try:
        order = client.order.create(
            data={
                "amount": amount_paise,
                "currency": "INR",
                "receipt": booking.booking_number,
                "notes": {
                    "booking_id": str(booking.id),
                    "customer_id": str(current_user.id),
                },
            }
        )
    except Exception:
        logger.exception("Razorpay order creation failed")
        raise HTTPException(
            502,
            "Could not create Razorpay order",
        )

    payment = Payment(
        booking_id=booking.id,
        transaction_id=f"ORDER-{order['id']}",
        amount=booking.grand_total,
        payment_method="Razorpay",
        status="Created",
        razorpay_order_id=order["id"],
    )

    try:
        db.add(payment)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not save Razorpay order")
        raise HTTPException(
            500,
            "Could not save payment order",
        )

    return {
        "booking_id": booking.id,
        "razorpay_order_id": order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "key_id": RAZORPAY_KEY_ID,
    }


# ---------------------------------------------------------
# RAZORPAY SIGNATURE VERIFICATION
# ---------------------------------------------------------

@router.post("/{booking_id}/verify-payment")
async def verify_razorpay_payment(
    booking_id: int,
    payment_data: RazorpayVerifyRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Customer")),
):
    booking = (
        db.query(Booking)
        .filter(
            Booking.id == booking_id,
            Booking.customer_id == current_user.id,
        )
        .first()
    )

    if not booking:
        raise HTTPException(404, "Booking not found")

    payment = (
        db.query(Payment)
        .filter(
            Payment.booking_id == booking.id,
            Payment.razorpay_order_id == payment_data.razorpay_order_id,
            Payment.payment_method == "Razorpay",
        )
        .first()
    )

    if not payment:
        raise HTTPException(404, "Razorpay order not found")

    if payment.status == "Success":
        return {
            "message": "Payment already verified",
            "booking_status": booking.status,
            "payment_status": payment.status,
        }

    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        (
            payment_data.razorpay_order_id
            + "|"
            + payment_data.razorpay_payment_id
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected_signature,
        payment_data.razorpay_signature,
    ):
        raise HTTPException(
            400,
            "Invalid Razorpay payment signature",
        )

    payment.razorpay_payment_id = payment_data.razorpay_payment_id

    try:
        confirm_successful_payment(
            db,
            payment,
            background_tasks,
        )
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("Razorpay payment confirmation failed")
        raise HTTPException(
            500,
            "Could not confirm payment",
        )

    db.refresh(payment)
    db.refresh(booking)

    if payment.status == "Success":
        seat_ids = get_booking_seat_ids(db, booking.id)

        await broadcast_booking_seats(
            booking.show_id,
            seat_ids,
        )

        send_ticket_notifications(
            booking,
            db,
            background_tasks,
        )

        return {
            "message": "Payment verified successfully",
            "booking_id": booking.id,
            "booking_number": booking.booking_number,
            "payment_status": payment.status,
            "booking_status": booking.status,
        }

    return {
        "message": "Payment received but requires manual review",
        "booking_id": booking.id,
        "payment_status": payment.status,
        "booking_status": booking.status,
    }


# ---------------------------------------------------------
# RAZORPAY WEBHOOK
# ---------------------------------------------------------

@router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(
        default=None,
        alias="X-Razorpay-Signature",
    ),
    db: Session = Depends(get_db),
):
    if not RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(
            503,
            "Razorpay webhook secret is not configured",
        )

    if not x_razorpay_signature:
        raise HTTPException(
            400,
            "Missing Razorpay webhook signature",
        )

    body = await request.body()

    expected_signature = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(
        expected_signature,
        x_razorpay_signature,
    ):
        raise HTTPException(
            400,
            "Invalid webhook signature",
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid webhook JSON")

    event = payload.get("event")

    if event not in ("payment.captured", "payment.failed"):
        return {
            "message": "Webhook event ignored",
            "event": event,
        }

    payment_entity = (
        payload.get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )

    order_id = payment_entity.get("order_id")
    razorpay_payment_id = payment_entity.get("id")

    if not order_id or not razorpay_payment_id:
        raise HTTPException(
            400,
            "Webhook payment information is incomplete",
        )

    payment = (
        db.query(Payment)
        .filter(
            Payment.razorpay_order_id == order_id,
            Payment.payment_method == "Razorpay",
        )
        .first()
    )

    if not payment:
        logger.warning(
            "Webhook received for unknown Razorpay order %s",
            order_id,
        )
        return {"message": "Unknown order"}

    if payment.status == "Success":
        return {"message": "Payment already processed"}

    if event == "payment.failed":
        if payment.status not in ("Created", "Failed"):
            return {"message": "Payment already processed"}

        payment.razorpay_payment_id = razorpay_payment_id
        payment.status = "Failed"

        notify_customer(
            db,
            payment.booking.customer_id,
            f"Payment failed for booking "
            f"{payment.booking.booking_number}.",
            "Payment Failed",
        )

        db.commit()
        return {"message": "Payment failure recorded"}

    if event == "payment.captured":
        # Verify the actual captured amount before confirmation.
        captured_amount = payment_entity.get("amount")
        expected_amount = int(
            (
                Decimal(str(payment.amount)) * 100
            ).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )

        if captured_amount != expected_amount:
            logger.error(
                "Amount mismatch for Razorpay order %s",
                order_id,
            )
            payment.status = "Review Required"
            db.commit()
            return {"message": "Payment amount mismatch; review required"}

        payment.razorpay_payment_id = razorpay_payment_id

        confirm_successful_payment(
            db,
            payment,
            None,
        )

        return {
            "message": "Captured payment processed",
            "payment_status": payment.status,
        }


# ---------------------------------------------------------
# PAYMENT HISTORY
# ---------------------------------------------------------

@router.get("/{booking_id}/payments")
def get_booking_payments(
    booking_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(
        require_roles("Customer", "Organizer", "Admin")
    ),
):
    booking = get_accessible_booking(
        booking_id,
        current_user,
        db,
    )

    payments = (
        db.query(Payment)
        .filter(Payment.booking_id == booking.id)
        .order_by(Payment.id.desc())
        .all()
    )

    return [
        {
            "id": payment.id,
            "booking_id": payment.booking_id,
            "transaction_id": payment.transaction_id,
            "razorpay_order_id": payment.razorpay_order_id,
            "razorpay_payment_id": payment.razorpay_payment_id,
            "amount": float(payment.amount),
            "payment_method": payment.payment_method,
            "status": payment.status,
            "paid_at": payment.paid_at,
        }
        for payment in payments
    ]
