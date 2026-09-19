import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str | None = None
    avatar_url: str | None = None
    is_active: bool
    is_email_verified: bool
    design_profile: dict
    created_at: datetime
    updated_at: datetime

    # Contact & delivery address
    phone: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    # Privacy preferences
    model_improvement_consent: bool = False
    buyer_signal_sharing: bool = True
    nominee_name: str | None = None
    nominee_contact: str | None = None

    # Notification preferences
    push_notifications: bool = True
    marketing_consent: bool = True

    # User Credits
    credit_balance: float = 20.0

    @property
    def profile_complete(self) -> bool:
        """True when the user has provided at minimum a phone and city."""
        return bool(self.phone and self.city)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str | None = Field(default=None, max_length=255)
    avatar_url: str | None = Field(default=None, max_length=1024)
    phone: str | None = Field(default=None, max_length=20)
    address_line1: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    pincode: str | None = Field(default=None, max_length=20)
    latitude: float | None = Field(default=None)
    longitude: float | None = Field(default=None)
    model_improvement_consent: bool | None = Field(default=None)
    buyer_signal_sharing: bool | None = Field(default=None)
    nominee_name: str | None = Field(default=None, max_length=255)
    nominee_contact: str | None = Field(default=None, max_length=255)
    push_notifications: bool | None = Field(default=None)
    marketing_consent: bool | None = Field(default=None)
    is_email_verified: bool | None = Field(default=None)
