import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.product import ProductOut


class ChatAnalyzeRequest(BaseModel):
    session_id: uuid.UUID
    image_base64: str = Field(min_length=10)
    media_type: str = Field(default="image/jpeg", max_length=64)
    # Optional style template the user picked from the home discovery feed.
    # `style_name` is what the chat surfaces to the user; `style_vibe` is the
    # short editorial blurb the AI uses to ground its analysis.
    style_slug: str | None = Field(default=None, max_length=64)
    style_name: str | None = Field(default=None, max_length=128)
    style_vibe: str | None = Field(default=None, max_length=512)


class ChatRequest(BaseModel):
    session_id: uuid.UUID
    # Either content or an image (or both) must be present — validated in
    # the route handler. min_length is 0 here so callers can send a
    # standalone image attachment without a text caption.
    content: str = Field(default="", max_length=4000)
    # Optional inline image — when set, the route persists it via
    # /upload/room-image semantics and attaches it to the user message.
    image_base64: str | None = Field(default=None, min_length=10)
    media_type: str = Field(default="image/jpeg", max_length=64)


class ProductCarouselPayload(BaseModel):
    type: Literal["product_carousel"] = "product_carousel"
    products: list[ProductOut]


class RoomPreviewPayload(BaseModel):
    type: Literal["room_preview"] = "room_preview"
    image_id: uuid.UUID
    product_id: uuid.UUID | None = None
    source_image_id: uuid.UUID | None = None
    design_summary: dict[str, Any] | None = None


UIPayload = ProductCarouselPayload | RoomPreviewPayload


class ChatResponse(BaseModel):
    message_id: uuid.UUID
    content: str
    ui_payload: dict[str, Any] | None = None
    created_at: datetime
    # Set on the /chat/analyze response when a style was selected — points
    # at the background image-gen job that's producing a styled makeover
    # of the user's room. The client polls /visualize/{task_id} to know
    # when the room_preview message lands in the session.
    makeover_task_id: uuid.UUID | None = None
    # Set when a normal chat turn asks the image model to create something.
    # The client polls the existing /visualize/{task_id} status endpoint.
    image_task_id: uuid.UUID | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    ui_payload: dict[str, Any] | None
    image_id: uuid.UUID | None
    created_at: datetime
