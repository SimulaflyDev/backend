import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ── Buyer-facing input ───────────────────────────────────────────────────────

class OrderItemIn(BaseModel):
    """Single line-item in a buyer lead submission."""
    product_id: uuid.UUID
    variant_id: uuid.UUID | None = None
    qty: int = Field(default=1, ge=1)
    price_at_capture: Decimal
    title: str
    img_url: str | None = None
    sku: str


class BuyerLeadCreate(BaseModel):
    """Flutter submits this to POST /buyer/leads/."""
    merchant_product_id: uuid.UUID
    session_id: str | None = None
    delivery_city: str | None = Field(default=None, max_length=100)
    delivery_phone: str | None = Field(default=None, max_length=50)
    delivery_address_line1: str | None = Field(default=None, max_length=255)
    delivery_state: str | None = Field(default=None, max_length=100)
    delivery_pincode: str | None = Field(default=None, max_length=20)
    delivery_latitude: float | None = None
    delivery_longitude: float | None = None
    coupon_code: str | None = Field(default=None, max_length=50)
    # Kept for backwards-compatible clients; the server always recalculates it.
    discount_amount: Decimal | None = None
    items: list[OrderItemIn] = Field(default_factory=list)


# ── Merchant-facing input ────────────────────────────────────────────────────

class CancellationReason(BaseModel):
    """Structured reason a merchant provides when cancelling an order."""
    parent_reason: str
    child_reason: str
    note: str | None = None


class BuyerLeadUpdate(BaseModel):
    """Merchant PATCH — update status and/or notes."""
    status: str | None = None   # new|synced|converted|lost
    merchant_notes: str | None = None
    cancellation_reason: CancellationReason | None = None
    fulfillment_status: Literal["pending", "in_progress", "shipped", "out_for_delivery", "fulfilled", "cancelled"] | None = None
    payment_status: Literal["paid"] | None = None


# ── Shared output types ───────────────────────────────────────────────────────

class CustomerInfo(BaseModel):
    """Buyer contact info; PII fields are None when status is 'new'."""
    city: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    address_line1: str | None = None
    state: str | None = None
    pincode: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class OrderMerchantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    display_name: str
    legal_name: str
    slug: str
    shop_id: str | None = None
    partner_id: str | None = None
    address: str | None = None
    support_phone: str | None = None
    support_email: str | None = None


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    items: list[dict]
    subtotal_estimated: Decimal
    total_estimated: Decimal
    coupon_code: str | None = None
    discount_amount: Decimal
    accepted_at: datetime | None = None
    fee_charged_at: datetime | None = None
    platform_fee_amount: Decimal
    completed_at: datetime | None = None
    fulfillment_status: str = "pending"
    payment_status: str = "unknown"
    payment_completed_at: datetime | None = None
    shipped_at: datetime | None = None
    out_for_delivery_at: datetime | None = None
    delivered_at: datetime | None = None
    reward_granted_at: datetime | None = None
    reward_tokens: int = 0
    delivery_address: dict = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class BuyerLeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    merchant_id: uuid.UUID
    lead_type: str
    status: str
    estimated_value: Decimal
    ai_interactions_count: int
    ai_generated_image_url: str | None = None
    delivery_city: str | None = None
    merchant_notes: str | None = None
    cancellation_reason: dict | None = None
    converted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    # Joined at query time — not ORM-native
    customer: CustomerInfo
    order: OrderOut | None = None
    merchant: OrderMerchantOut | None = None


class PaginatedLeads(BaseModel):
    items: list[BuyerLeadOut]
    total: int
    limit: int
    offset: int
