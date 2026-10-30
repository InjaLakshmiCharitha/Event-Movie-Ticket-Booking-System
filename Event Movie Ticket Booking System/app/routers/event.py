from datetime import datetime, timedelta, date, time
from decimal import Decimal

from sqlalchemy import func

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user, require_roles
from app.database import get_db
from app.models.models import (
    Event,
    Show,
    Screen,
    Venue,
    SeatCategoryPrice,
    User
)
from app.schemas.event import (
    EventCreate,
    EventUpdate,
    EventResponse,
    ShowCreate,
    ShowUpdate,
    ShowResponse,
)
from app.redis_client import cache_get, cache_set, cache_delete_pattern

router = APIRouter(tags=["Events & Shows"])

CACHE_TTL = 300


def invalidate_event_cache(event_id: int | None = None):
    """Invalidate event and show caches safely."""
    cache_delete_pattern("events:*")

    if event_id is not None:
        cache_delete_pattern(f"event:{event_id}:*")


def check_organizer(user: User):
    if user.role == "Organizer" and not user.is_approved:
        raise HTTPException(
            status_code=403,
            detail="Organizer account is not approved"
        )


def get_owned_event(event_id: int, user: User, db: Session):
    event = db.query(Event).filter(Event.id == event_id).first()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if user.role != "Admin" and event.organizer_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You cannot manage this event"
        )

    return event


def check_screen(screen_id: int, db: Session):
    screen = db.query(Screen).filter(Screen.id == screen_id).first()

    if not screen:
        raise HTTPException(status_code=404, detail="Screen not found")

    if not screen.is_active or not screen.venue.is_active:
        raise HTTPException(
            status_code=409,
            detail="Screen or venue is inactive"
        )

    return screen


def check_show_overlap(
    screen_id: int,
    start_time: datetime,
    end_time: datetime,
    db: Session,
    exclude_show_id: int | None = None,
):
    query = db.query(Show).filter(
        Show.screen_id == screen_id,
        Show.status == "Scheduled",
        Show.start_time < end_time + timedelta(minutes=15),
        Show.end_time > start_time - timedelta(minutes=15),
    )

    if exclude_show_id is not None:
        query = query.filter(Show.id != exclude_show_id)

    if query.first():
        raise HTTPException(
            status_code=409,
            detail="This show overlaps another show or its 15-minute cleaning gap",
        )


@router.post("/events", response_model=EventResponse, status_code=201)
def create_event(
    payload: EventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Organizer", "Admin")),
):
    check_organizer(current_user)

    event = Event(
        organizer_id=current_user.id,
        title=payload.title.strip(),
        category=payload.category.strip(),
        language=payload.language.strip(),
        description=payload.description,
        duration_minutes=payload.duration_minutes,
        age_rating=payload.age_rating.strip(),
        status="Draft",
    )

    db.add(event)
    db.commit()
    db.refresh(event)

    invalidate_event_cache()

    return event



@router.get("/events", response_model=list[EventResponse])
def list_events(
    skip: int = 0,
    limit: int = 10,
    title: str | None = None,
    city: str | None = None,
    category: str | None = None,
    language: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    sort_by: str = "title",
    sort_order: str = "asc",
    db: Session = Depends(get_db),
):
    # Pagination validation
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

    # Date validation
    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="Start date cannot be after end date",
        )

    # Price validation
    if min_price is not None and min_price < 0:
        raise HTTPException(
            status_code=400,
            detail="Minimum price cannot be negative",
        )

    if max_price is not None and max_price < 0:
        raise HTTPException(
            status_code=400,
            detail="Maximum price cannot be negative",
        )

    if (
        min_price is not None
        and max_price is not None
        and min_price > max_price
    ):
        raise HTTPException(
            status_code=400,
            detail="Minimum price cannot exceed maximum price",
        )

    # Sorting validation
    allowed_sort_fields = ["title", "show_date", "price"]

    if sort_by not in allowed_sort_fields:
        raise HTTPException(
            status_code=400,
            detail="sort_by must be title, show_date, or price",
        )

    if sort_order.lower() not in ["asc", "desc"]:
        raise HTTPException(
            status_code=400,
            detail="sort_order must be asc or desc",
        )

    # Build a unique cache key for each filter combination.
    cache_key = (
        f"events:list:{skip}:{limit}:"
        f"{title}:{city}:{category}:{language}:"
        f"{start_date}:{end_date}:"
        f"{min_price}:{max_price}:"
        f"{sort_by}:{sort_order.lower()}"
    )

    cached_events = cache_get(cache_key)

    if cached_events is not None:
        return cached_events

    # Only published events are visible.
    query = db.query(Event).filter(
        Event.status == "Published"
    )

    # Title filter
    if title:
        query = query.filter(
            Event.title.ilike(f"%{title.strip()}%")
        )

    # Category filter
    if category:
        query = query.filter(
            Event.category.ilike(category.strip())
        )

    # Language filter
    if language:
        query = query.filter(
            Event.language.ilike(language.strip())
        )

    # City filter through Show -> Screen -> Venue
    if city:
        city_exists = (
            db.query(Show.id)
            .join(Screen, Show.screen_id == Screen.id)
            .join(Venue, Screen.venue_id == Venue.id)
            .filter(
                Show.event_id == Event.id,
                Venue.city.ilike(city.strip()),
            )
            .exists()
        )

        query = query.filter(city_exists)

    # Date range filter
    if start_date or end_date:
        date_query = db.query(Show.id).filter(
            Show.event_id == Event.id
        )

        if start_date:
            date_query = date_query.filter(
                Show.show_date >= datetime.combine(
                    start_date, time.min
                )
            )

        if end_date:
            date_query = date_query.filter(
                Show.show_date <= datetime.combine(
                    end_date, time.max
                )
            )

        query = query.filter(date_query.exists())

    # Price range filter
    if min_price is not None or max_price is not None:
        price_query = (
            db.query(SeatCategoryPrice.id)
            .join(
                Show,
                SeatCategoryPrice.show_id == Show.id,
            )
            .filter(Show.event_id == Event.id)
        )

        if min_price is not None:
            price_query = price_query.filter(
                SeatCategoryPrice.price >= min_price
            )

        if max_price is not None:
            price_query = price_query.filter(
                SeatCategoryPrice.price <= max_price
            )

        query = query.filter(price_query.exists())

    # Sorting using related show and price records.
    show_date_subquery = (
        db.query(func.min(Show.show_date))
        .filter(Show.event_id == Event.id)
        .correlate(Event)
        .scalar_subquery()
    )

    price_subquery = (
        db.query(func.min(SeatCategoryPrice.price))
        .join(
            Show,
            SeatCategoryPrice.show_id == Show.id,
        )
        .filter(Show.event_id == Event.id)
        .correlate(Event)
        .scalar_subquery()
    )

    if sort_by == "title":
        sort_column = Event.title
    elif sort_by == "show_date":
        sort_column = show_date_subquery
    else:
        sort_column = price_subquery

    if sort_order.lower() == "desc":
        query = query.order_by(sort_column.desc())
    else:
        query = query.order_by(sort_column.asc())

    # Pagination
    events = (
        query
        .offset(skip)
        .limit(limit)
        .all()
    )

    # Cache JSON-compatible response data for five minutes.
    event_data = [
        EventResponse.model_validate(event).model_dump(mode="json")
        for event in events
    ]

    cache_set(cache_key, event_data, CACHE_TTL)

    return event_data


@router.get("/events/{event_id}", response_model=EventResponse)
def get_event(event_id: int, db: Session = Depends(get_db)):
    cache_key = f"events:detail:{event_id}"

    cached_event = cache_get(cache_key)

    if cached_event is not None:
        return cached_event

    event = db.query(Event).filter(Event.id == event_id).first()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    event_data = EventResponse.model_validate(event).model_dump(mode="json")

    cache_set(cache_key, event_data, CACHE_TTL)

    return event


@router.put("/events/{event_id}", response_model=EventResponse)
def update_event(
    event_id: int,
    payload: EventUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Organizer", "Admin")),
):
    check_organizer(current_user)
    event = get_owned_event(event_id, current_user, db)

    changes = payload.model_dump(exclude_unset=True)

    if "duration_minutes" in changes and changes["duration_minutes"] is not None:
        if changes["duration_minutes"] != event.duration_minutes:
            if db.query(Show).filter(Show.event_id == event.id).first():
                raise HTTPException(
                    status_code=409,
                    detail="Cannot change event duration after shows have been created",
                )

    for field, value in changes.items():
        if isinstance(value, str) and field in {
            "title", "category", "language", "age_rating"
        }:
            value = value.strip()

        setattr(event, field, value)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Event update conflicts with existing data"
        )

    db.refresh(event)

    invalidate_event_cache(event_id)

    return event


@router.post(
    "/events/{event_id}/shows",
    response_model=ShowResponse,
    status_code=201,
)
def create_show(
    event_id: int,
    payload: ShowCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Organizer", "Admin")),
):
    check_organizer(current_user)
    event = get_owned_event(event_id, current_user, db)

    if event.status == "Cancelled":
        raise HTTPException(
            status_code=409,
            detail="Cannot schedule a show for a cancelled event"
        )

    screen = check_screen(payload.screen_id, db)
    end_time = payload.start_time + timedelta(minutes=event.duration_minutes)

    check_show_overlap(
        screen_id=screen.id,
        start_time=payload.start_time,
        end_time=end_time,
        db=db,
    )

    show = Show(
        event_id=event.id,
        screen_id=screen.id,
        show_date=payload.show_date,
        start_time=payload.start_time,
        end_time=end_time,
        booking_closes_at=payload.start_time - timedelta(minutes=15),
        status=payload.status,
        reminder_sent=False,
    )

    db.add(show)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Show conflicts with existing data"
        )

    db.refresh(show)

    invalidate_event_cache(event_id)

    return show


@router.get(
    "/events/{event_id}/shows",
    response_model=list[ShowResponse]
)
def list_event_shows(
    event_id: int,
    db: Session = Depends(get_db),
):
    cache_key = f"events:{event_id}:shows"

    cached_shows = cache_get(cache_key)

    if cached_shows is not None:
        return cached_shows

    event = db.query(Event).filter(Event.id == event_id).first()

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    shows = (
        db.query(Show)
        .filter(Show.event_id == event_id)
        .order_by(Show.start_time)
        .all()
    )

    show_data = [
        ShowResponse.model_validate(show).model_dump(mode="json")
        for show in shows
    ]

    cache_set(cache_key, show_data, CACHE_TTL)

    return shows


@router.put("/shows/{show_id}", response_model=ShowResponse)
def update_show(
    show_id: int,
    payload: ShowUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Organizer", "Admin")),
):
    check_organizer(current_user)

    show = db.query(Show).filter(Show.id == show_id).first()

    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    event = get_owned_event(show.event_id, current_user, db)

    if show.status == "Cancelled":
        raise HTTPException(
            status_code=409,
            detail="Cancelled shows cannot be edited"
        )

    changes = payload.model_dump(exclude_unset=True)

    new_screen_id = changes.get("screen_id", show.screen_id)
    new_start_time = changes.get("start_time", show.start_time)
    new_show_date = changes.get("show_date", show.show_date)
    new_status = changes.get("status", show.status)

    if new_start_time <= datetime.now() and (
        new_start_time != show.start_time or new_status == "Scheduled"
    ):
        raise HTTPException(
            status_code=422,
            detail="Show start time must be in the future"
        )

    if new_show_date.date() != new_start_time.date():
        raise HTTPException(
            status_code=422,
            detail="Show date must match the start time date"
        )

    check_screen(new_screen_id, db)

    new_end_time = new_start_time + timedelta(minutes=event.duration_minutes)

    if new_status == "Scheduled":
        check_show_overlap(
            screen_id=new_screen_id,
            start_time=new_start_time,
            end_time=new_end_time,
            db=db,
            exclude_show_id=show.id,
        )

    show.screen_id = new_screen_id
    show.show_date = new_show_date
    show.start_time = new_start_time
    show.end_time = new_end_time
    show.booking_closes_at = new_start_time - timedelta(minutes=15)
    show.status = new_status

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Show update conflicts with existing data"
        )

    db.refresh(show)

    invalidate_event_cache(event.id)

    return show


@router.put("/shows/{show_id}/cancel", response_model=ShowResponse)
def cancel_show(
    show_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Organizer", "Admin")),
):
    check_organizer(current_user)

    show = db.query(Show).filter(Show.id == show_id).first()

    if not show:
        raise HTTPException(status_code=404, detail="Show not found")

    event = get_owned_event(show.event_id, current_user, db)

    show.status = "Cancelled"
    db.commit()
    db.refresh(show)

    invalidate_event_cache(event.id)

    return show

