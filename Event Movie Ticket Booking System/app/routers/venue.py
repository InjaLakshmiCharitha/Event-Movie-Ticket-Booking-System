from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import Venue, Screen, Seat
from app.auth.dependencies import require_roles
from app.schemas.venue import (
    VenueCreate,
    VenueUpdate,
    VenueResponse,
    ScreenCreate,
    ScreenResponse,
    BulkSeatCreate,
    SeatStatusUpdate,
    SeatResponse,
)

router = APIRouter(tags=["Venues, Screens & Seats"])
admin_required = require_roles("Admin")


@router.post(
    "/venues",
    response_model=VenueResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_venue(
    payload: VenueCreate,
    db: Session = Depends(get_db),
    admin=Depends(admin_required),
):
    venue = Venue(**payload.model_dump())
    db.add(venue)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Could not create venue")

    db.refresh(venue)
    return venue



@router.get("/venues", response_model=list[VenueResponse])
def get_venues(
    skip: int = 0,
    limit: int = 10,
    db: Session = Depends(get_db),
    admin=Depends(admin_required),
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

    return (
        db.query(Venue)
        .order_by(Venue.id)
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.put("/venues/{venue_id}", response_model=VenueResponse)
def update_venue(
    venue_id: int,
    payload: VenueUpdate,
    db: Session = Depends(get_db),
    admin=Depends(admin_required),
):
    venue = db.query(Venue).filter(Venue.id == venue_id).first()

    if venue is None:
        raise HTTPException(status_code=404, detail="Venue not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(venue, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Could not update venue")

    db.refresh(venue)
    return venue


@router.post(
    "/venues/{venue_id}/screens",
    response_model=ScreenResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_screen(
    venue_id: int,
    payload: ScreenCreate,
    db: Session = Depends(get_db),
    admin=Depends(admin_required),
):
    venue = db.query(Venue).filter(Venue.id == venue_id).first()

    if venue is None:
        raise HTTPException(status_code=404, detail="Venue not found")

    if not venue.is_active:
        raise HTTPException(
            status_code=409,
            detail="Cannot add a screen to an inactive venue",
        )

    screen = Screen(
        venue_id=venue_id,
        name=payload.name.strip(),
        total_seats=0,
        is_active=True,
    )
    db.add(screen)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A screen with this name already exists at the venue",
        )

    db.refresh(screen)
    return screen


@router.post(
    "/screens/{screen_id}/seats/bulk",
    status_code=status.HTTP_201_CREATED,
)
def create_seats_bulk(
    screen_id: int,
    payload: BulkSeatCreate,
    db: Session = Depends(get_db),
    admin=Depends(admin_required),
):
    screen = db.query(Screen).filter(Screen.id == screen_id).first()

    if screen is None:
        raise HTTPException(status_code=404, detail="Screen not found")

    if not screen.is_active:
        raise HTTPException(
            status_code=409,
            detail="Cannot add seats to an inactive screen",
        )

    all_rows = [
        row
        for group in payload.row_groups
        for row in group.rows
    ]

    if len(all_rows) != len(set(all_rows)):
        raise HTTPException(
            status_code=422,
            detail="A row label cannot appear in multiple row groups",
        )

    new_seats = [
        Seat(
            screen_id=screen_id,
            row_label=row,
            seat_number=number,
            category=group.category,
            is_active=True,
        )
        for group in payload.row_groups
        for row in group.rows
        for number in range(1, group.seats_per_row + 1)
    ]

    requested_positions = {
        (seat.row_label, seat.seat_number)
        for seat in new_seats
    }
    existing_rows = {row for row, _ in requested_positions}

    existing_seats = (
        db.query(Seat)
        .filter(
            Seat.screen_id == screen_id,
            Seat.row_label.in_(existing_rows),
        )
        .all()
    )

    conflicts = sorted(
        f"{seat.row_label}{seat.seat_number}"
        for seat in existing_seats
        if (seat.row_label, seat.seat_number) in requested_positions
    )

    if conflicts:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Some seats already exist. No seats were created.",
                "conflicting_seats": conflicts,
            },
        )

    existing_count = (
        db.query(Seat)
        .filter(Seat.screen_id == screen_id)
        .count()
    )

    try:
        db.add_all(new_seats)
        screen.total_seats = existing_count + len(new_seats)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A seat conflict occurred. No seats were created.",
        )
    except Exception:
        db.rollback()
        raise

    return {
        "message": "Seats created successfully",
        "screen_id": screen_id,
        "created_count": len(new_seats),
        "total_seats": screen.total_seats,
    }


@router.get("/screens/{screen_id}/layout")
def get_screen_layout(
    screen_id: int,
    db: Session = Depends(get_db),
    admin=Depends(admin_required),
):
    screen = db.query(Screen).filter(Screen.id == screen_id).first()

    if screen is None:
        raise HTTPException(status_code=404, detail="Screen not found")

    seats = (
        db.query(Seat)
        .filter(Seat.screen_id == screen_id)
        .order_by(Seat.row_label, Seat.seat_number)
        .all()
    )

    grouped = defaultdict(list)

    for seat in seats:
        grouped[seat.row_label].append(
            {
                "id": seat.id,
                "seat_number": seat.seat_number,
                "category": seat.category,
                "status": "Available" if seat.is_active else "Unavailable",
                "is_active": seat.is_active,
            }
        )

    return {
        "screen_id": screen.id,
        "screen_name": screen.name,
        "venue_id": screen.venue_id,
        "total_seats": screen.total_seats,
        "rows": [
            {
                "row_label": row,
                "seats": grouped[row],
            }
            for row in sorted(grouped)
        ],
    }


@router.put("/seats/{seat_id}/status", response_model=SeatResponse)
def update_seat_status(
    seat_id: int,
    payload: SeatStatusUpdate,
    db: Session = Depends(get_db),
    admin=Depends(admin_required),
):
    seat = db.query(Seat).filter(Seat.id == seat_id).first()

    if seat is None:
        raise HTTPException(status_code=404, detail="Seat not found")

    if not seat.screen.is_active:
        raise HTTPException(
            status_code=409,
            detail="Cannot change the status of a seat on an inactive screen",
        )

    seat.is_active = payload.is_active

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(seat)
    return seat

