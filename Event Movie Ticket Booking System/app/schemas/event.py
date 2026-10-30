from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
    field_validator,
)


EventStatus = Literal["Draft", "Published", "Cancelled"]
ShowStatus = Literal["Scheduled", "Cancelled"]


class EventCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    category: str = Field(min_length=2, max_length=100)
    language: str = Field(min_length=2, max_length=50)
    description: str | None = None
    duration_minutes: int = Field(ge=30, le=300)
    age_rating: str = Field(min_length=1, max_length=20)

    @field_validator("title", "category", "language", "age_rating")
    @classmethod
    def validate_required_text(cls, value: str):
        value = value.strip()

        if not value:
            raise ValueError("This field cannot be empty")

        return value


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=200)
    category: str | None = Field(default=None, min_length=2, max_length=100)
    language: str | None = Field(default=None, min_length=2, max_length=50)
    description: str | None = None
    duration_minutes: int | None = Field(default=None, ge=30, le=300)
    age_rating: str | None = Field(default=None, min_length=1, max_length=20)
    status: EventStatus | None = None

    @field_validator("title", "category", "language", "age_rating")
    @classmethod
    def validate_optional_text(cls, value: str | None):
        if value is not None:
            value = value.strip()

            if not value:
                raise ValueError("This field cannot be empty")

        return value


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organizer_id: int
    title: str
    category: str
    language: str
    description: str | None
    duration_minutes: int
    age_rating: str
    status: str


class ShowCreate(BaseModel):
    screen_id: int = Field(gt=0)
    show_date: datetime
    start_time: datetime
    status: ShowStatus = "Scheduled"

    @model_validator(mode="after")
    def validate_show_times(self):
        if self.start_time <= datetime.now():
            raise ValueError("Show start time must be in the future")

        if self.show_date.date() != self.start_time.date():
            raise ValueError("Show date must match the start time date")

        return self


class ShowUpdate(BaseModel):
    screen_id: int | None = Field(default=None, gt=0)
    show_date: datetime | None = None
    start_time: datetime | None = None
    status: ShowStatus | None = None


class ShowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: int
    screen_id: int
    show_date: datetime
    start_time: datetime
    end_time: datetime
    booking_closes_at: datetime
    status: str
    reminder_sent: bool
