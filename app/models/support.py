import enum
import secrets
import string
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SupportRequesterType(str, enum.Enum):
    MERCHANT = "merchant"
    CONSUMER = "consumer"


class SupportTicketStatus(str, enum.Enum):
    OPEN = "open"
    PENDING = "pending"
    RESOLVED = "resolved"


class SupportTicketPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class SupportMessageAuthorType(str, enum.Enum):
    ADMIN = "admin"
    REQUESTER = "requester"


SUPPORT_REFERENCE_ALPHABET = string.ascii_uppercase + string.digits


def new_support_reference() -> str:
    return "".join(secrets.choice(SUPPORT_REFERENCE_ALPHABET) for _ in range(6))


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    __table_args__ = (UniqueConstraint("requester_type", "requester_id", "client_request_id", name="uq_support_request"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(
        String(6), unique=True, nullable=False, index=True, default=new_support_reference
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    requester_type: Mapped[str] = mapped_column(String(16), nullable=False)
    requester_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SupportTicketStatus.OPEN.value
    )
    priority: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SupportTicketPriority.MEDIUM.value
    )
    sla_due_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # ── Structured merchant ticket metadata ────────────────────────────────────
    # reason / sub_reason store the parent/child category slugs from the taxonomy.
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sub_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Optional reference to a specific merchant product (SET NULL on product delete).
    merchant_product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("merchant_products.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # URL of the attached screenshot / image (stored via upload endpoint).
    attachment_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Free-text description submitted by the merchant.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("buyer_leads.id", ondelete="SET NULL"), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("design_sessions.id", ondelete="SET NULL"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    messages = relationship(
        "SupportMessage",
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="SupportMessage.created_at",
    )
    merchant_product = relationship(
        "MerchantProduct",
        foreign_keys=[merchant_product_id],
    )


class SupportMessage(Base):
    __tablename__ = "support_messages"
    __table_args__ = (UniqueConstraint("ticket_id", "client_message_id", name="uq_support_message_request"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support_tickets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_type: Mapped[str] = mapped_column(String(16), nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    client_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    ticket = relationship("SupportTicket", back_populates="messages")


class SupportSettings(Base):
    __tablename__ = "support_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_support_settings_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False)
