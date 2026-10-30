from typing import Literal
from datetime import datetime

from pydantic import (
    BaseModel,
    Field,
    ConfigDict,
    field_validator,
)


class BookingCreate(BaseModel):
    show_id: int = Field(gt=0)
    seat_ids: list[int] = Field(min_length=1, max_length=10)
    coupon_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=50,
    )

    @field_validator("seat_ids")
    @classmethod
    def validate_seat_ids(cls, seat_ids: list[int]) -> list[int]:
        if any(seat_id <= 0 for seat_id in seat_ids):
            raise ValueError("Seat IDs must be positive integers")
        if len(seat_ids) != len(set(seat_ids)):
            raise ValueError("Duplicate seat IDs are not allowed")
        return seat_ids


class BookingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    booking_number: str
    customer_id: int
    show_id: int
    seat_count: int
    ticket_total: float
    convenience_fee: float
    gst_amount: float
    grand_total: float
    status: str


class MockPaymentRequest(BaseModel):
    payment_method: Literal["UPI", "Card", "Net Banking"]
    simulate_status: Literal["Success", "Failed"]


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    booking_id: int
    transaction_id: str
    amount: float
    payment_method: str
    status: str
    paid_at: datetime | None = None


class RazorpayOrderResponse(BaseModel):
    booking_id: int
    razorpay_order_id: str
    amount: int
    currency: str = "INR"
    key_id: str


class RazorpayVerifyRequest(BaseModel):
    razorpay_order_id: str = Field(min_length=1, max_length=100)
    razorpay_payment_id: str = Field(min_length=1, max_length=100)
    razorpay_signature: str = Field(min_length=1, max_length=255)
