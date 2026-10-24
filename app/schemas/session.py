import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SessionStatus = Literal["active", "archived"]


class SessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    room_image_id: uuid.UUID | None = None


class SessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: SessionStatus | None = None
    room_image_id: uuid.UUID | None = None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    status: str
    room_image_id: uuid.UUID | None
    # Frozen design context for this room (including the selected style). The
    # mobile chat uses this to rebuild its room header/summary after restart.
    profile_snapshot: dict
    context_summary: str | None
    created_at: datetime
    updated_at: datetime
