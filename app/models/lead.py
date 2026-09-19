import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LeadType(str, enum.Enum):
    DIRECT_PURCHASE = "direct_purchase"
    CART_ABANDONMENT = "cart_abandonment"
    HIGH_INTENT_VIEW = "high_intent_view"


class LeadStatus(str, enum.Enum):
    NEW = "new"
    SYNCED = "synced"
    CONVERTED = "converted"
    LOST = "lost"


class OrderStatus(str, enum.Enum):
    PENDING_MERCHANT_CONTACT = "pending_merchant_contact"
    CONTACTED = "contacted"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class DisputeStatus(str, enum.Enum):
    NONE = "none"
    OPEN = "open"
    RESOLVED = "resolved"


class FulfillmentStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SHIPPED = "shipped"
    OUT_FOR_DELIVERY = "out_for_delivery"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"


class BuyerLead(Base):
    __tablename__ = "buyer_leads"
    __table_args__ = (
        Index(
            "ix_buyer_leads_merchant_status_created",
            "merchant_id", "status", "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lead_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LeadStatus.NEW.value
    )
    # JSON array of merchant_product UUIDs (as strings)
    product_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    ai_interactions_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    ai_generated_image_url: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    estimated_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    delivery_city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    delivery_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    merchant_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stores {parent_reason, child_reason, note} when merchant cancels
    cancellation_reason: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index(
            "ix_orders_merchant_status_created",
            "merchant_id", "status", "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("buyer_leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=OrderStatus.PENDING_MERCHANT_CONTACT.value,
    )
    # [{product_id, variant_id, qty, price_at_capture, title, img_url, sku}]
    items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    total_estimated: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    subtotal_estimated: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchant_coupons.id", ondelete="SET NULL"),
        nullable=True,
    )
    coupon_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    delivery_address: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    merchant_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    payment_status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", nullable=False)
    payment_completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    out_for_delivery_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    reward_granted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    reward_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    merchant_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    fee_charged_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    platform_fee_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0")
    )

    # --- Admin booking operations (additive, nullable/defaulted) ---
    # Dispute tracking (none|open|resolved)
    dispute_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DisputeStatus.NONE.value
    )
    dispute_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispute_resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fulfillment tracking (pending|in_progress|fulfilled|cancelled)
    fulfillment_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=FulfillmentStatus.PENDING.value
    )
    # Soft-delete marker
    deleted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
