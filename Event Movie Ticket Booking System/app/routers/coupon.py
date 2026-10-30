from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth.dependencies import require_roles
from app.models.models import Coupon, CouponUsage, User
from app.schemas.coupon import (
    CouponCreate,
    CouponUpdate,
    CouponResponse,
)

router = APIRouter(
    prefix="/coupons",
    tags=["Coupon Management"],
)


def validate_discount(
    discount_type: str,
    discount_value,
):
    discount_type = discount_type.strip().lower()

    if discount_type not in (
        "percentage",
        "percent",
        "fixed",
        "amount",
    ):
        raise HTTPException(
            status_code=400,
            detail="Discount type must be percentage or fixed",
        )

    if discount_value <= 0:
        raise HTTPException(
            status_code=400,
            detail="Discount value must be greater than zero",
        )

    if discount_type in ("percentage", "percent"):
        if discount_value > 100:
            raise HTTPException(
                status_code=400,
                detail="Percentage discount cannot exceed 100",
            )


def get_coupon_or_404(
    coupon_id: int,
    db: Session,
):
    coupon = db.query(Coupon).filter(
        Coupon.id == coupon_id
    ).first()

    if not coupon:
        raise HTTPException(
            status_code=404,
            detail="Coupon not found",
        )

    return coupon


# CREATE COUPON

@router.post(
    "",
    response_model=CouponResponse,
    status_code=201,
)
def create_coupon(
    payload: CouponCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Admin")),
):
    code = payload.code.strip().upper()
    discount_type = payload.discount_type.strip().lower()

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Coupon code cannot be empty",
        )

    validate_discount(
        discount_type,
        payload.discount_value,
    )

    existing = db.query(Coupon).filter(
        func.lower(Coupon.code) == code.lower()
    ).first()

    if existing:
        raise HTTPException(
            status_code=409,
            detail="Coupon code already exists",
        )

    if payload.expires_at and payload.expires_at <= datetime.now():
        raise HTTPException(
            status_code=400,
            detail="Coupon expiry must be in the future",
        )

    coupon = Coupon(
        code=code,
        discount_type=discount_type,
        discount_value=payload.discount_value,
        min_order_amount=payload.min_order_amount,
        min_ticket_count=payload.min_ticket_count,
        max_uses=payload.max_uses,
        expires_at=payload.expires_at,
        is_active=payload.is_active,
    )

    try:
        db.add(coupon)
        db.commit()
        db.refresh(coupon)

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Coupon code already exists",
        )

    return coupon


# GET ALL COUPONS

@router.get(
    "",
    response_model=list[CouponResponse],
)
def get_all_coupons(
    skip: int = 0,
    limit: int = 20,
    active_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Admin")),
):
    if skip < 0 or limit < 1 or limit > 100:
        raise HTTPException(
            status_code=400,
            detail="Invalid pagination values",
        )

    query = db.query(Coupon)

    if active_only:
        query = query.filter(
            Coupon.is_active.is_(True)
        )

    return query.order_by(
        Coupon.id.desc()
    ).offset(skip).limit(limit).all()


# GET COUPON BY ID

@router.get(
    "/{coupon_id}",
    response_model=CouponResponse,
)
def get_coupon(
    coupon_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Admin")),
):
    return get_coupon_or_404(coupon_id, db)


# UPDATE COUPON

@router.put(
    "/{coupon_id}",
    response_model=CouponResponse,
)
def update_coupon(
    coupon_id: int,
    payload: CouponUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Admin")),
):
    coupon = get_coupon_or_404(coupon_id, db)

    updates = payload.model_dump(exclude_unset=True)

    if not updates:
        raise HTTPException(
            status_code=400,
            detail="No fields provided for update",
        )

    if "code" in updates:
        code = updates["code"].strip().upper()

        if not code:
            raise HTTPException(
                status_code=400,
                detail="Coupon code cannot be empty",
            )

        existing = db.query(Coupon).filter(
            func.lower(Coupon.code) == code.lower(),
            Coupon.id != coupon_id,
        ).first()

        if existing:
            raise HTTPException(
                status_code=409,
                detail="Coupon code already exists",
            )

        updates["code"] = code

    new_type = updates.get(
        "discount_type",
        coupon.discount_type,
    )

    new_value = updates.get(
        "discount_value",
        coupon.discount_value,
    )

    if "discount_type" in updates:
        new_type = new_type.strip().lower()
        updates["discount_type"] = new_type

    validate_discount(new_type, new_value)

    if (
        "expires_at" in updates
        and updates["expires_at"] is not None
        and updates["expires_at"] <= datetime.now()
    ):
        raise HTTPException(
            status_code=400,
            detail="Coupon expiry must be in the future",
        )

    for field, value in updates.items():
        setattr(coupon, field, value)

    try:
        db.commit()
        db.refresh(coupon)

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Coupon update conflicts with an existing coupon",
        )

    return coupon


# DELETE COUPON

@router.delete("/{coupon_id}")
def delete_coupon(
    coupon_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("Admin")),
):
    coupon = get_coupon_or_404(coupon_id, db)

    usage_count = db.query(CouponUsage).filter(
        CouponUsage.coupon_id == coupon_id
    ).count()

    if usage_count > 0:
        coupon.is_active = False
        db.commit()

        return {
            "message": "Coupon has been deactivated because it has already been used",
            "coupon_id": coupon.id,
            "is_active": False,
        }

    db.delete(coupon)
    db.commit()

    return {
        "message": "Coupon deleted successfully",
        "coupon_id": coupon_id,
    }