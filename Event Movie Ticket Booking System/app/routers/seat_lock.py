from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.dependencies import require_roles
from app.database import get_db
from app.models.models import (
    Booking,
    BookingSeat,
    Seat,
    SeatCategoryPrice,
    Show,
    User,
)
from app.redis_client import redis_client, SEAT_LOCK_TTL, enforce_rate_limit
from app.schemas.seat_lock import SeatLockRequest
from app.routers.seat_websocket import publish_seat_updates

router = APIRouter(tags=["Seat Availability & Locks"])

LOCK_SCRIPT = """
for i, key in ipairs(KEYS) do
    local owner = redis.call('GET', key)
    if owner and owner ~= ARGV[1] then
        return 0
    end
end
for i, key in ipairs(KEYS) do
    redis.call('SET', key, ARGV[1], 'EX', tonumber(ARGV[2]))
end
return 1
"""

RELEASE_SCRIPT = """
local released = 0
for i, key in ipairs(KEYS) do
    if redis.call('GET', key) == ARGV[1] then
        redis.call('DEL', key)
        released = released + 1
    end
end
return released
"""


def get_show(show_id: int, db: Session) -> Show:
    show = db.query(Show).filter(Show.id == show_id).first()

    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    return show


def check_booking_open(show: Show):
    if show.status != "Scheduled":
        raise HTTPException(
            status_code=409,
            detail="This show is not available for booking",
        )

    if show.booking_closes_at <= datetime.now():
        raise HTTPException(
            status_code=409,
            detail="Booking has closed for this show",
        )


def lock_key(show_id: int, seat_id: int) -> str:
    return f"lock:show:{show_id}:seat:{seat_id}"


def get_booked_seat_ids(show_id: int, db: Session) -> set[int]:
    rows = (
        db.query(BookingSeat.seat_id)
        .join(Booking, Booking.id == BookingSeat.booking_id)
        .filter(
            BookingSeat.show_id == show_id,
            Booking.status.in_(["Pending Payment", "Confirmed"]),
        )
        .all()
    )

    return {row[0] for row in rows}


def get_seat_data(show: Show, db: Session) -> list[dict]:
    seats = (
        db.query(Seat)
        .filter(Seat.screen_id == show.screen_id)
        .order_by(Seat.row_label, Seat.seat_number)
        .all()
    )

    prices = {
        item.category: item.price
        for item in db.query(SeatCategoryPrice)
        .filter(SeatCategoryPrice.show_id == show.id)
        .all()
    }

    booked_ids = get_booked_seat_ids(show.id, db)
    result = []

    for seat in seats:
        owner = redis_client.get(lock_key(show.id, seat.id))

        if not seat.is_active:
            seat_status = "Unavailable"
        elif seat.id in booked_ids:
            seat_status = "Booked"
        elif owner:
            seat_status = "Locked"
        else:
            seat_status = "Available"

        result.append(
            {
                "seat_id": seat.id,
                "row_label": seat.row_label,
                "seat_number": seat.seat_number,
                "category": seat.category,
                "price": prices.get(seat.category),
                "status": seat_status,
                "lock_owner": owner,
            }
        )

    return result


def enforce_single_empty_seat(
    seats: list[dict],
    selected_ids: set[int],
):
    rows: dict[str, list[dict]] = {}

    for seat in seats:
        rows.setdefault(seat["row_label"], []).append(seat)

    for row_seats in rows.values():
        row_seats.sort(key=lambda seat: seat["seat_number"])

        for index, seat in enumerate(row_seats):
            if (
                seat["status"] != "Available"
                or seat["seat_id"] in selected_ids
            ):
                continue

            left_selected = (
                index > 0
                and row_seats[index - 1]["seat_id"] in selected_ids
            )
            right_selected = (
                index + 1 < len(row_seats)
                and row_seats[index + 1]["seat_id"] in selected_ids
            )

            if left_selected or right_selected:
                left_unavailable = (
                    index == 0
                    or row_seats[index - 1]["status"] != "Available"
                    or row_seats[index - 1]["seat_id"] in selected_ids
                )
                right_unavailable = (
                    index == len(row_seats) - 1
                    or row_seats[index + 1]["status"] != "Available"
                    or row_seats[index + 1]["seat_id"] in selected_ids
                )

                if left_unavailable and right_unavailable:
                    raise HTTPException(
                        status_code=409,
                        detail="Selection would leave a single empty seat in a row",
                    )


def get_customer_locked_seat_ids(show_id: int, customer_id: int) -> set[int]:
    keys = redis_client.scan_iter(match=f"lock:show:{show_id}:seat:*")
    owned_seat_ids = set()

    for key in keys:
        if redis_client.get(key) == str(customer_id):
            owned_seat_ids.add(int(key.rsplit(":", 1)[1]))

    return owned_seat_ids


@router.get("/shows/{show_id}/seats")
def list_show_seats(
    show_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Customer", "Admin")),
):
    show = get_show(show_id, db)

    try:
        seats = get_seat_data(show, db)
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Seat availability service is unavailable",
        )

    for seat in seats:
        if seat["lock_owner"] == str(current_user.id):
            seat["status"] = "Yours"

        seat.pop("lock_owner", None)

    return {
        "show_id": show.id,
        "event_id": show.event_id,
        "screen_id": show.screen_id,
        "booking_closes_at": show.booking_closes_at,
        "seats": seats,
    }


@router.post("/shows/{show_id}/lock-seats")
async def lock_show_seats(
    show_id: int,
    payload: SeatLockRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Customer")),
):
    # Maximum 5 seat-lock requests per customer per minute
    enforce_rate_limit(
        key=f"seat_lock:{current_user.id}",
        max_requests=5,
        window_seconds=60
    )

    show = get_show(show_id, db)
    check_booking_open(show)

    seats = (
        db.query(Seat)
        .filter(
            Seat.screen_id == show.screen_id,
            Seat.id.in_(payload.seat_ids),
        )
        .all()
    )

    if len(seats) != len(payload.seat_ids):
        raise HTTPException(
            status_code=404,
            detail="One or more seats do not exist for this screen",
        )

    if any(not seat.is_active for seat in seats):
        raise HTTPException(
            status_code=409,
            detail="One or more selected seats are unavailable",
        )

    booked_ids = get_booked_seat_ids(show.id, db)

    if booked_ids.intersection(payload.seat_ids):
        raise HTTPException(
            status_code=409,
            detail="One or more selected seats are already booked",
        )

    try:
        owned_seat_ids = get_customer_locked_seat_ids(
            show.id,
            current_user.id,
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Seat locking service is unavailable",
        )

    if len(owned_seat_ids.union(payload.seat_ids)) > 10:
        raise HTTPException(
            status_code=409,
            detail="A maximum of 10 seats can be locked per customer per show",
        )

    try:
        seat_map = get_seat_data(show, db)
        enforce_single_empty_seat(seat_map, set(payload.seat_ids))

        keys = [
            lock_key(show.id, seat_id)
            for seat_id in payload.seat_ids
        ]

        locked = redis_client.eval(
            LOCK_SCRIPT,
            len(keys),
            *keys,
            str(current_user.id),
            SEAT_LOCK_TTL,
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Seat locking service is unavailable",
        )

    if not locked:
        raise HTTPException(
            status_code=409,
            detail="One or more seats are locked by another customer",
        )

    await publish_seat_updates(
        show.id,
        payload.seat_ids,
        "Locked",
    )

    return {
        "message": "Seats locked successfully",
        "show_id": show.id,
        "seat_ids": payload.seat_ids,
        "expires_in_seconds": SEAT_LOCK_TTL,
        "locked_until": datetime.now() + timedelta(seconds=SEAT_LOCK_TTL),
    }


@router.delete("/shows/{show_id}/lock-seats")
async def release_show_seats(
    show_id: int,
    payload: SeatLockRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Customer")),
):
    show = get_show(show_id, db)

    seats = (
        db.query(Seat)
        .filter(
            Seat.screen_id == show.screen_id,
            Seat.id.in_(payload.seat_ids),
        )
        .all()
    )

    if len(seats) != len(payload.seat_ids):
        raise HTTPException(
            status_code=404,
            detail="One or more seats do not exist for this screen",
        )

    keys = [
        lock_key(show.id, seat_id)
        for seat_id in payload.seat_ids
    ]

    try:
        released_count = redis_client.eval(
            RELEASE_SCRIPT,
            len(keys),
            *keys,
            str(current_user.id),
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Seat locking service is unavailable",
        )

    if released_count:
        await publish_seat_updates(
            show.id,
            payload.seat_ids,
            "Available",
        )

    return {
        "message": "Your seat locks have been released",
        "show_id": show.id,
        "requested_seat_ids": payload.seat_ids,
        "released_count": int(released_count),
    }


@router.get("/shows/{show_id}/suggest-seats")
def suggest_adjacent_seats(
    show_id: int,
    count: int,
    category: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Customer")),
):
    if count < 1 or count > 10:
        raise HTTPException(
            status_code=400,
            detail="Seat count must be between 1 and 10",
        )

    if not category.strip():
        raise HTTPException(
            status_code=400,
            detail="Seat category is required",
        )

    show = get_show(show_id, db)
    check_booking_open(show)

    # Get active seats in the requested category
    seats = (
        db.query(Seat)
        .filter(
            Seat.screen_id == show.screen_id,
            Seat.category == category.strip(),
            Seat.is_active.is_(True),
        )
        .order_by(Seat.row_label, Seat.seat_number)
        .all()
    )

    if not seats:
        raise HTTPException(
            status_code=404,
            detail="No active seats found in this category",
        )

    # Get already booked seats
    booked_ids = get_booked_seat_ids(show.id, db)

    # Remove seats locked by any customer
    available_seats = []

    try:
        for seat in seats:
            if seat.id in booked_ids:
                continue

            owner = redis_client.get(lock_key(show.id, seat.id))

            if owner:
                continue

            available_seats.append(seat)

    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Seat availability service is unavailable",
        )

    # Group available seats by row
    rows: dict[str, list[Seat]] = {}

    for seat in available_seats:
        rows.setdefault(seat.row_label, []).append(seat)

    # Find consecutive adjacent seats
    suggestions = []

    for row_label, row_seats in rows.items():
        row_seats.sort(key=lambda seat: seat.seat_number)

        for index in range(len(row_seats) - count + 1):
            selected = row_seats[index:index + count]

            # Seats must be consecutive in the same row
            consecutive = all(
                selected[i].seat_number + 1 == selected[i + 1].seat_number
                for i in range(len(selected) - 1)
            )

            if consecutive:
                suggestions.append({
                    "row_label": row_label,
                    "seats": selected,
                })

    if not suggestions:
        raise HTTPException(
            status_code=404,
            detail=f"No {count} adjacent seats available in category {category}",
        )

    # Prefer seats closest to the middle of the row
    all_row_seats = (
        db.query(Seat)
        .filter(
            Seat.screen_id == show.screen_id,
            Seat.is_active.is_(True),
        )
        .all()
    )

    row_centers = {}

    for seat in all_row_seats:
        row_centers.setdefault(seat.row_label, []).append(seat.seat_number)

    def suggestion_score(item):
        row_numbers = row_centers.get(item["row_label"], [])
        row_middle = (min(row_numbers) + max(row_numbers)) / 2

        selected_numbers = [
            seat.seat_number for seat in item["seats"]
        ]
        selected_middle = (
            min(selected_numbers) + max(selected_numbers)
        ) / 2

        return (
            abs(selected_middle - row_middle),
            item["row_label"],
            selected_numbers[0],
        )

    best = min(suggestions, key=suggestion_score)

    prices = {
        item.category: item.price
        for item in db.query(SeatCategoryPrice)
        .filter(SeatCategoryPrice.show_id == show.id)
        .all()
    }

    return {
        "message": "Best adjacent seats found",
        "show_id": show.id,
        "category": category,
        "requested_count": count,
        "suggested_seats": [
            {
                "seat_id": seat.id,
                "row_label": seat.row_label,
                "seat_number": seat.seat_number,
                "category": seat.category,
                "price": prices.get(seat.category),
            }
            for seat in best["seats"]
        ],
    }