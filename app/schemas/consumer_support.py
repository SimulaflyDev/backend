import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class HelpFAQ(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=160)
    answer: str = Field(min_length=1, max_length=2000)


class HelpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    version: int = Field(default=0, ge=0)
    title: str = Field(default="We're here to help", min_length=1, max_length=100)
    introduction: str = Field(default="Send us a question and follow your conversation here.", max_length=1000)
    response_time: str = Field(default="", max_length=200)
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32, pattern=r"^\+?[0-9 ()-]{6,30}$")
    accepting_tickets: bool = True
    faqs: list[HelpFAQ] = Field(default_factory=list, max_length=12)


class CustomerTicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_request_id: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    subject: str = Field(min_length=3, max_length=160)
    category: Literal["order", "payment", "ai_design", "account", "app", "other"] = "other"
    description: str = Field(min_length=5, max_length=4000)
    lead_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None


class CustomerMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_message_id: str = Field(min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    body: str = Field(min_length=1, max_length=4000)


class CustomerMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    author_type: str
    body: str
    created_at: datetime


class CustomerTicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    reference: str
    subject: str
    reason: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    lead_id: uuid.UUID | None
    session_id: uuid.UUID | None


class CustomerTicketDetail(CustomerTicketOut):
    messages: list[CustomerMessageOut]


class CustomerTicketPage(BaseModel):
    items: list[CustomerTicketOut]
    total: int
    limit: int
    offset: int
