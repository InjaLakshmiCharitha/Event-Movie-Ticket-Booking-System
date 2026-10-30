import secrets
from datetime import datetime, timedelta, time, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth.dependencies import require_roles
from app.models.models import (
    User,
    Show,
    Seat,
    SeatCategoryPrice,
    Booking,
    BookingSeat,
    Coupon,
    CouponUsage,
)
from app.redis_client import redis_client
from app.routers.seat_websocket import publish_seat_updates
from app.schemas.booking import (
    BookingCreate,
    BookingResponse,
)

router = APIRouter(prefix="/bookings", tags=["Bookings"])

CONVENIENCE_FEE_PER_TICKET = Decimal("30.00")
GST_RATE = Decimal("0.18")

# Dynamic pricing levels.
# 90% or more occupancy = 20% price increase.
# 70% or more occupancy = 10% price increase.
# Below 70% occupancy = original price.
DYNAMIC_PRICE_LEVELS = [
    (Decimal("0.90"), Decimal("1.20")),
    (Decimal("0.70"), Decimal("1.10")),
]

RELEASE_OWN_LOCK_SCRIPT = """
for i, key in ipairs(KEYS) do
    if redis.call('GET', key) == ARGV[1] then
        redis.call('DEL', key)
    end
end
return 1
"""


def money(value: Decimal) -> Decimal:
    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def seat_lock_key(show_id: int, seat_id: int) -> str:
    return f"lock:show:{show_id}:seat:{seat_id}"


def release_customer_locks(
    show_id: int,
    seat_ids: list[int],
    customer_id: int,
) -> None:
    keys = [
        seat_lock_key(show_id, seat_id)
        for seat_id in seat_ids
    ]

    if keys:
        redis_client.eval(
            RELEASE_OWN_LOCK_SCRIPT,
            len(keys),
            *keys,
            str(customer_id),
        )


def free_booking_seats(
    db: Session,
    booking_id: int,
) -> None:
    db.query(BookingSeat).filter(
        BookingSeat.booking_id == booking_id
    ).delete(synchronize_session=False)

    db.query(CouponUsage).filter(
        CouponUsage.booking_id == booking_id
    ).delete(synchronize_session=False)


def get_accessible_booking(
    booking_id: int,
    current_user: User,
    db: Session,
) -> Booking:
    booking = db.query(Booking).filter(
        Booking.id == booking_id
    ).first()

    if not booking:
        raise HTTPException(
            status_code=404,
            detail="Booking not found",
        )

    if current_user.role == "Admin":
        return booking

    if current_user.role == "Organizer":
        show = db.query(Show).filter(
            Show.id == booking.show_id
        ).first()

        if show and show.event.organizer_id == current_user.id:
            return booking

        raise HTTPException(
            status_code=403,
            detail="You cannot access bookings for another organizer's event",
        )

    if booking.customer_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="You cannot access this booking",
        )

    return booking


def get_booking_query(
    current_user: User,
    db: Session,
):
    query = db.query(Booking)

    if current_user.role == "Admin":
        return query

    if current_user.role == "Organizer":
        return query.join(
            Show,
            Booking.show_id == Show.id,
        ).filter(
            Show.event.has(
                organizer_id=current_user.id
            )
        )

    return query.filter(
        Booking.customer_id == current_user.id
    )


@router.post(
    "",
    response_model=BookingResponse,
    status_code=201,
)
async def create_booking(
    payload: BookingCreate,
    current_user: User = Depends(
        require_roles("Customer")
    ),
    db: Session = Depends(get_db),
):
    now = datetime.now()

    if not payload.seat_ids:
        raise HTTPException(
            status_code=400,
            detail="Select at least one seat",
        )

    if len(payload.seat_ids) != len(set(payload.seat_ids)):
        raise HTTPException(
            status_code=400,
            detail="Duplicate seat IDs are not allowed",
        )

    show = db.query(Show).filter(
        Show.id == payload.show_id
    ).first()

    if not show:
        raise HTTPException(
            status_code=404,
            detail="Show not found",
        )

    if show.status != "Scheduled":
        raise HTTPException(
            status_code=400,
            detail="This show is not available for booking",
        )

    if now >= show.booking_closes_at:
        raise HTTPException(
            status_code=400,
            detail="The booking window has closed",
        )

    seats = db.query(Seat).filter(
        Seat.id.in_(payload.seat_ids),
        Seat.screen_id == show.screen_id,
        Seat.is_active.is_(True),
    ).all()

    if len(seats) != len(payload.seat_ids):
        raise HTTPException(
            status_code=400,
            detail="One or more seats are invalid or inactive for this show",
        )

    # Verify that every selected seat is locked by this customer.
    lock_ttls = []

    for seat_id in payload.seat_ids:
        key = seat_lock_key(show.id, seat_id)
        owner = redis_client.get(key)

        if owner != str(current_user.id):
            raise HTTPException(
                status_code=409,
                detail=f"Seat {seat_id} is not locked by you or its lock has expired",
            )

        ttl = redis_client.ttl(key)

        if ttl <= 0:
            raise HTTPException(
                status_code=409,
                detail=f"Seat {seat_id} lock has expired",
            )

        lock_ttls.append(ttl)

    # Booking expiry cannot exceed the earliest seat-lock expiry.
    expires_at = now + timedelta(
        seconds=min(lock_ttls)
    )

    # Check whether selected seats are already booked.
    booked_seat_ids = {
        row[0]
        for row in db.query(
            BookingSeat.seat_id
        ).join(
            Booking,
            Booking.id == BookingSeat.booking_id,
        ).filter(
            BookingSeat.show_id == show.id,
            Booking.status.in_(
                ["Pending Payment", "Confirmed"]
            ),
            BookingSeat.seat_id.in_(payload.seat_ids),
        ).all()
    }

    if booked_seat_ids:
        raise HTTPException(
            status_code=409,
            detail="One or more selected seats are already booked",
        )

    # Get configured prices for this show's seat categories.
    category_prices = {
        row.category: row.price
        for row in db.query(SeatCategoryPrice).filter(
            SeatCategoryPrice.show_id == show.id
        ).all()
    }

   
    # Count all active seats in the show's screen.
    total_seats = db.query(Seat).filter(
        Seat.screen_id == show.screen_id,
        Seat.is_active.is_(True),
    ).count()

    # Count seats in pending-payment and confirmed bookings.
    occupied_seats = db.query(
        func.count(BookingSeat.id)
    ).join(
        Booking,
        Booking.id == BookingSeat.booking_id,
    ).filter(
        BookingSeat.show_id == show.id,
        Booking.status.in_(
            ["Pending Payment", "Confirmed"]
        ),
    ).scalar() or 0

    occupancy_rate = (
        Decimal(occupied_seats) / Decimal(total_seats)
        if total_seats > 0
        else Decimal("0")
    )

    # Determine the price multiplier.
    price_multiplier = Decimal("1.00")

    for threshold, multiplier in DYNAMIC_PRICE_LEVELS:
        if occupancy_rate >= threshold:
            price_multiplier = multiplier
            break

    # Apply dynamic pricing to selected seats.
    seat_prices = []

    for seat in seats:
        price = category_prices.get(seat.category)

        if price is None:
            raise HTTPException(
                status_code=400,
                detail=f"No price configured for seat category {seat.category}",
            )

        base_price = money(
            Decimal(str(price))
        )

        if base_price <= Decimal("0.00"):
            raise HTTPException(
                status_code=400,
                detail=f"Price for seat category {seat.category} must be greater than zero",
            )

        seat_price = money(
            base_price * price_multiplier
        )

        seat_prices.append(
            (seat, seat_price)
        )

    # Calculate total ticket price using dynamic prices.
    ticket_total = money(
        sum(
            (price for _, price in seat_prices),
            Decimal("0.00"),
        )
    )

  
    discount_amount = Decimal("0.00")
    coupon = None

    if payload.coupon_code:
        coupon = db.query(Coupon).filter(
            func.lower(Coupon.code)
            == payload.coupon_code.strip().lower()
        ).first()

        if not coupon or not coupon.is_active:
            raise HTTPException(
                status_code=400,
                detail="Coupon is invalid or inactive",
            )

        if coupon.expires_at and now >= coupon.expires_at:
            raise HTTPException(
                status_code=400,
                detail="Coupon has expired",
            )

        if ticket_total < Decimal(
            str(coupon.min_order_amount)
        ):
            raise HTTPException(
                status_code=400,
                detail="Minimum order amount not reached for this coupon",
            )

        if len(payload.seat_ids) < coupon.min_ticket_count:
            raise HTTPException(
                status_code=400,
                detail="Minimum ticket count not reached for this coupon",
            )

        if coupon.max_uses is not None:
            usage_count = db.query(
                CouponUsage
            ).filter(
                CouponUsage.coupon_id == coupon.id
            ).count()

            if usage_count >= coupon.max_uses:
                raise HTTPException(
                    status_code=400,
                    detail="Coupon usage limit reached",
                )

        discount_type = coupon.discount_type.strip().lower()

        discount_value = Decimal(
            str(coupon.discount_value)
        )

        if discount_type in ("percentage", "percent"):
            if not Decimal("0") <= discount_value <= Decimal("100"):
                raise HTTPException(
                    status_code=400,
                    detail="Coupon percentage must be between 0 and 100",
                )

            discount_amount = money(
                ticket_total
                * discount_value
                / Decimal("100")
            )

        elif discount_type in ("fixed", "amount"):
            if discount_value < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Coupon discount cannot be negative",
                )

            discount_amount = money(
                discount_value
            )

        else:
            raise HTTPException(
                status_code=400,
                detail="Unsupported coupon discount type",
            )

        discount_amount = min(
            discount_amount,
            ticket_total,
        )

   
    discounted_total = money(
        ticket_total - discount_amount
    )

    # Convenience fee: ₹30 per ticket.
    convenience_fee = money(
        CONVENIENCE_FEE_PER_TICKET
        * len(seat_prices)
    )

    # GST: 18% on discounted ticket total + convenience fee.
    gst_amount = money(
        (discounted_total + convenience_fee)
        * GST_RATE
    )

    grand_total = money(
        discounted_total
        + convenience_fee
        + gst_amount
    )

    
    today = now.strftime("%Y%m%d")
    prefix = f"BKG-{today}-"

    last_booking = db.query(Booking).filter(
        Booking.booking_number.like(
            f"{prefix}%"
        )
    ).order_by(
        Booking.booking_number.desc()
    ).first()

    sequence = 1

    if last_booking:
        try:
            sequence = (
                int(
                    last_booking.booking_number.rsplit("-", 1)[1]
                ) + 1
            )
        except (ValueError, IndexError):
            sequence = 1

    booking = Booking(
        booking_number=f"{prefix}{sequence:04d}",
        customer_id=current_user.id,
        show_id=show.id,
        seat_count=len(seat_prices),
        ticket_total=ticket_total,
        convenience_fee=convenience_fee,
        gst_amount=gst_amount,
        grand_total=grand_total,
        status="Pending Payment",
        expires_at=expires_at,
    )

    try:
        db.add(booking)
        db.flush()

        for seat, price in seat_prices:
            db.add(
                BookingSeat(
                    booking_id=booking.id,
                    show_id=show.id,
                    seat_id=seat.id,
                    price=price,
                    ticket_code=secrets.token_urlsafe(24),
                )
            )

        if coupon and discount_amount > 0:
            db.add(
                CouponUsage(
                    coupon_id=coupon.id,
                    booking_id=booking.id,
                    discount_amount=discount_amount,
                )
            )

        db.commit()
        db.refresh(booking)

    except IntegrityError:
        db.rollback()

        raise HTTPException(
            status_code=409,
            detail="A selected seat or booking number conflicts with another booking. Please retry.",
        )

    # Release Redis locks after successful booking creation.
    release_customer_locks(
        show.id,
        payload.seat_ids,
        current_user.id,
    )

    # Notify connected clients about seat updates.
    await publish_seat_updates(
        show.id,
        payload.seat_ids,
        "Booked",
    )

    return booking



@router.get(
    "",
    response_model=list[BookingResponse],
)
def get_bookings(
    skip: int = 0,
    limit: int = 10,
    status_filter: Optional[str] = None,
    show_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: User = Depends(
        require_roles("Customer", "Organizer", "Admin")
    ),
    db: Session = Depends(get_db),
):
    if skip < 0:
        raise HTTPException(
            status_code=400,
            detail="Skip cannot be negative",
        )

    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=400,
            detail="Limit must be between 1 and 100",
        )

    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="Start date cannot be after end date",
        )

    query = get_booking_query(
        current_user,
        db,
    )

    if status_filter:
        query = query.filter(
            Booking.status == status_filter
        )

    if show_id is not None:
        query = query.filter(
            Booking.show_id == show_id
        )

    if start_date:
        query = query.filter(
            Booking.created_at >= datetime.combine(
                start_date,
                time.min,
            )
        )

    if end_date:
        query = query.filter(
            Booking.created_at <= datetime.combine(
                end_date,
                time.max,
            )
        )

    return query.order_by(
        Booking.id.desc()
    ).offset(skip).limit(limit).all()


@router.get(
    "/{booking_id}",
    response_model=BookingResponse,
)
def get_booking(
    booking_id: int,
    current_user: User = Depends(
        require_roles("Customer", "Organizer", "Admin")
    ),
    db: Session = Depends(get_db),
):
    return get_accessible_booking(
        booking_id,
        current_user,
        db,
    )

