from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SeatCategory = Literal["Silver", "Gold", "Recliner"]


class VenueCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    address: str = Field(min_length=5, max_length=255)
    city: str = Field(min_length=2, max_length=100)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    is_active: bool = True


class VenueUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    address: str | None = Field(default=None, min_length=5, max_length=255)
    city: str | None = Field(default=None, min_length=2, max_length=100)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    is_active: bool | None = None


class VenueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: str
    city: str
    latitude: Decimal | None
    longitude: Decimal | None
    is_active: bool


class ScreenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ScreenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    venue_id: int
    name: str
    total_seats: int
    is_active: bool


class SeatRowGroup(BaseModel):
    rows: list[str] = Field(min_length=1)
    seats_per_row: int = Field(gt=0, le=100)
    category: SeatCategory

    @field_validator("rows")
    @classmethod
    def validate_rows(cls, rows: list[str]) -> list[str]:
        cleaned = [row.strip().upper() for row in rows]

        if any(not row or len(row) > 10 for row in cleaned):
            raise ValueError("Each row label must contain 1 to 10 characters")

        if len(cleaned) != len(set(cleaned)):
            raise ValueError("Duplicate row labels are not allowed")

        return cleaned


class BulkSeatCreate(BaseModel):
    row_groups: list[SeatRowGroup] = Field(min_length=1)


class SeatStatusUpdate(BaseModel):
    is_active: bool


class SeatResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    screen_id: int
    row_label: str
    seat_number: int
    category: str
    is_active: bool
