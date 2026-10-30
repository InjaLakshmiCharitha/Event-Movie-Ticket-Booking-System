from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)


class UserRegister(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    role: Literal["Customer", "Organizer"] = "Customer"
    phone: str | None = None

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str):
        value = value.strip()

        if len(value) < 2:
            raise ValueError("Full name must contain at least 2 characters")

        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None):
        if value is not None:
            value = value.strip()

            if len(value) != 10 or not value.isdigit():
                raise ValueError(
                    "Phone number must contain exactly 10 digits"
                )

        return value


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    full_name: str
    email: EmailStr
    role: str
    phone: str | None = None
    is_active: bool
    is_approved: bool
    assigned_venue_id: int | None = None

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class StaffCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    phone: str
    assigned_venue_id: int = Field(gt=0)

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, value: str):
        value = value.strip()

        if len(value) < 2:
            raise ValueError("Full name must contain at least 2 characters")

        return value

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str):
        value = value.strip()

        if len(value) != 10 or not value.isdigit():
            raise ValueError(
                "Phone number must contain exactly 10 digits"
            )

        return value
