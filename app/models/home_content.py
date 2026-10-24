import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class HomeCarousel(Base):
    __tablename__ = "home_carousel"
    __table_args__ = (CheckConstraint("id = 1", name="ck_home_carousel_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class HomeCarouselImage(Base):
    """Public marketing assets, separate from private customer room images."""
    __tablename__ = "home_carousel_images"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    media_type: Mapped[str] = mapped_column(String(40), nullable=False, default="image/jpeg")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
