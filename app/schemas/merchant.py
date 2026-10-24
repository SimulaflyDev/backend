import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_validator

MemberRoleLiteral = Literal["owner", "admin", "staff"]
MerchantStatusLiteral = Literal["active", "suspended", "trial"]


def _validate_storefront_settings(settings: dict | None) -> dict | None:
    """Validate the merchant-controlled public storefront portion of settings."""
    if settings is None:
        return None
    storefront = settings.get("storefront")
    if storefront is None:
        return settings
    if not isinstance(storefront, dict):
        raise ValueError("settings.storefront must be an object")

    tagline = storefront.get("tagline")
    if tagline is not None and (not isinstance(tagline, str) or len(tagline.strip()) > 120):
        raise ValueError("storefront tagline must be at most 120 characters")
    hero_image_url = storefront.get("hero_image_url")
    if hero_image_url is not None and (
        not isinstance(hero_image_url, str) or len(hero_image_url.strip()) > 2048
    ):
        raise ValueError("storefront hero image URL must be at most 2048 characters")
    featured = storefront.get("featured_categories", [])
    if not isinstance(featured, list) or len(featured) > 3:
        raise ValueError("storefront can feature at most 3 categories")
    if any(not isinstance(item, str) or not item.strip() or len(item.strip()) > 80 for item in featured):
        raise ValueError("featured category names must be 1 to 80 characters")
    if len({item.strip().casefold() for item in featured}) != len(featured):
        raise ValueError("featured categories must be unique")
    return settings


class StorefrontSettingsOut(BaseModel):
    tagline: str | None = None
    description: str | None = None
    hero_image_url: str | None = None
    categories: list[str] = Field(default_factory=list)
    featured_categories: list[str] = Field(default_factory=list)


class MerchantCreate(BaseModel):
    legal_name: str = Field(min_length=2, max_length=255)
    display_name: str = Field(min_length=2, max_length=255)
    country: str = Field(default="IN", min_length=2, max_length=2)
    support_email: EmailStr | None = None
    support_phone: str | None = Field(default=None, max_length=50)
    logo_url: str | None = Field(default=None, max_length=1024)
    settings: dict | None = None
    # Location — collected at creation, immutable thereafter
    address: str | None = Field(default=None, max_length=1024)
    latitude: float | None = None
    longitude: float | None = None
    range_km: float | None = Field(default=None, ge=0)
    # ID generation inputs (optional, default: state_code="DL", city_code="N")
    state_code: str | None = Field(default="DL", min_length=2, max_length=2)
    city_code: str | None = Field(default="N", min_length=1, max_length=1)
    referred_by_code: str | None = None

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, settings: dict | None) -> dict | None:
        return _validate_storefront_settings(settings)


class MerchantUpdate(BaseModel):
    """Fields allowed to be updated after creation.

    NOTE: address, latitude, longitude are intentionally excluded.
    Location is set once at creation and cannot be changed via the API.
    Merchants must contact support@simulafly.com to request a location change.
    """
    legal_name: str | None = Field(default=None, min_length=2, max_length=255)
    display_name: str | None = Field(default=None, min_length=2, max_length=255)
    logo_url: str | None = Field(default=None, max_length=1024)
    support_email: EmailStr | None = None
    support_phone: str | None = Field(default=None, max_length=50)
    settings: dict | None = None
    range_km: float | None = Field(default=None, ge=0)
    @field_validator("settings")
    @classmethod
    def validate_settings(cls, settings: dict | None) -> dict | None:
        return _validate_storefront_settings(settings)


class MerchantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # Human-readable identifiers
    partner_id: str | None = None
    shop_id: str | None = None
    slug: str
    legal_name: str
    display_name: str
    logo_url: str | None
    support_email: str | None
    support_phone: str | None
    country: str
    status: MerchantStatusLiteral
    referral_code: str
    settings: dict
    # Location (read-only after creation)
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    range_km: float | None = None
    distance: float | None = None
    is_kyc_completed: bool
    referred_by_code: str | None = None
    referral_bonus_paid: bool
    created_at: datetime
    updated_at: datetime


class MerchantPublicOut(BaseModel):
    """Safe buyer-facing merchant profile without private operational settings."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    partner_id: str | None = None
    shop_id: str | None = None
    is_kyc_completed: bool = False
    slug: str
    legal_name: str
    display_name: str
    logo_url: str | None
    support_email: str | None
    support_phone: str | None
    country: str
    referral_code: str
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    range_km: float | None = None
    distance: float | None = None
    settings: dict = Field(default_factory=dict, exclude=True, repr=False)

    @computed_field
    @property
    def storefront(self) -> StorefrontSettingsOut:
        onboarding_value = self.settings.get("onboarding_data") or {}
        storefront_value = self.settings.get("storefront") or {}
        onboarding = onboarding_value if isinstance(onboarding_value, dict) else {}
        raw = storefront_value if isinstance(storefront_value, dict) else {}
        categories_value = onboarding.get("categories") or []
        categories = categories_value if isinstance(categories_value, list) else []
        featured_value = raw.get("featured_categories") or categories[:3]
        featured = featured_value if isinstance(featured_value, list) else []
        tagline = raw.get("tagline")
        description = raw.get("description") or onboarding.get("description")
        hero_image_url = raw.get("hero_image_url")
        return StorefrontSettingsOut(
            tagline=tagline if isinstance(tagline, str) else None,
            description=description if isinstance(description, str) else None,
            hero_image_url=hero_image_url if isinstance(hero_image_url, str) else None,
            categories=[str(item) for item in categories if item],
            featured_categories=[str(item) for item in featured if item][:3],
        )


class MemberInvite(BaseModel):
    email: EmailStr
    role: MemberRoleLiteral = "staff"


class MemberRoleUpdate(BaseModel):
    role: MemberRoleLiteral


class MerchantMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    full_name: str | None
    role: MemberRoleLiteral
    joined_at: datetime
