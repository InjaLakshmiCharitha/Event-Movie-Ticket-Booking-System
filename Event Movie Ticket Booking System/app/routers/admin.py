
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import get_db
from app.models.models import User, Venue
from app.schemas.auth import UserResponse, StaffCreate
from app.auth.security import hash_password
from app.auth.dependencies import require_roles

router = APIRouter(
    prefix="/admin",
    tags=["Admin"]
)


@router.put("/organizers/{user_id}/approve", response_model=UserResponse)
def approve_organizer(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_roles("Admin"))
):
    organizer = db.query(User).filter(
        User.id == user_id,
        User.role == "Organizer"
    ).first()

    if not organizer:
        raise HTTPException(
            status_code=404,
            detail="Organizer not found"
        )

    organizer.is_approved = True
    db.commit()
    db.refresh(organizer)

    return organizer


@router.post(
    "/staff",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED
)
def create_staff(
    staff_data: StaffCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_roles("Admin"))
):
    existing_user = db.query(User).filter(
        (User.email == staff_data.email)
        | (User.phone == staff_data.phone)
    ).first()

    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="Email or phone number is already registered"
        )

    venue = db.query(Venue).filter(
        Venue.id == staff_data.assigned_venue_id,
        Venue.is_active == True
    ).first()

    if not venue:
        raise HTTPException(
            status_code=404,
            detail="Active venue not found"
        )

    staff = User(
        full_name=staff_data.full_name,
        email=staff_data.email,
        password_hash=hash_password(staff_data.password),
        phone=staff_data.phone,
        role="Staff",
        is_active=True,
        is_approved=True,
        assigned_venue_id=staff_data.assigned_venue_id
    )

    db.add(staff)

    try:
        db.commit()
        db.refresh(staff)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Email or phone number is already registered"
        )

    return staff
