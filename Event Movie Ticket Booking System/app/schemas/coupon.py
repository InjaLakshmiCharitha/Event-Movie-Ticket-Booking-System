from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CouponCreate(BaseModel):
    code: str = Field(min_length=3, max_length=50)
    discount_type: str
    discount_value: Decimal = Field(gt=0)
    min_order_amount: Decimal = Field(
        default=Decimal("0"),
        ge=0,
    )
    min_ticket_count: int = Field(default=1, ge=1)
    max_uses: Optional[int] = Field(default=None, gt=0)
    expires_at: Optional[datetime] = None
    is_active: bool = True


class CouponUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=50,
    )
    discount_type: Optional[str] = None
    discount_value: Optional[Decimal] = Field(
        default=None,
        gt=0,
    )
    min_order_amount: Optional[Decimal] = Field(
        default=None,
        ge=0,
    )
    min_ticket_count: Optional[int] = Field(
        default=None,
        ge=1,
    )
    max_uses: Optional[int] = Field(
        default=None,
        gt=0,
    )
    expires_at: Optional[datetime] = None
    is_active: Optional[bool] = None


class CouponResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    discount_type: str
    discount_value: Decimal
    min_order_amount: Decimal
    min_ticket_count: int
    max_uses: Optional[int]
    expires_at: Optional[datetime]
    is_active: bool
    created_at: datetime
    updated_at: datetime