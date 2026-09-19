import uuid
from datetime import datetime

from sqlalchemy import Boolean, String, func, Float
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(String(1024))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    design_profile: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Contact & delivery address (collected via profile-setup flow in app)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pincode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Privacy preferences (DPDPA compliance & settings sync)
    model_improvement_consent: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    buyer_signal_sharing: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    nominee_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    nominee_contact: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Notification preferences (switch status sync)
    push_notifications: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # User Credits
    credit_balance: Mapped[float] = mapped_column(Float, default=20.0, nullable=False)
    referred_by_code: Mapped[str | None] = mapped_column(String(40), nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    sessions = relationship("DesignSession", back_populates="user", cascade="all, delete-orphan")
    cart_items = relationship("CartItem", back_populates="user", cascade="all, delete-orphan")
    room_images = relationship("RoomImage", back_populates="owner", cascade="all, delete-orphan")
    saved_items = relationship("SavedItem", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
