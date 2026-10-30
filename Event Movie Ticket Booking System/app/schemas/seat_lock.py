from pydantic import BaseModel, Field, field_validator


class SeatLockRequest(BaseModel):
    seat_ids: list[int] = Field(min_length=1, max_length=10)

    @field_validator("seat_ids")
    @classmethod
    def validate_seat_ids(cls, seat_ids: list[int]) -> list[int]:
        if any(seat_id <= 0 for seat_id in seat_ids):
            raise ValueError("Seat IDs must be positive integers")
        if len(seat_ids) != len(set(seat_ids)):
            raise ValueError("Duplicate seat IDs are not allowed")
        return seat_ids
