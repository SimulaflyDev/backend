"""Request/response schemas for the admin Support Center (Requirement 17).

The Support Center exposes a paginated/filterable ticket listing, a detail view
that carries the full message history, a respond action (appends a message), a
status-change action, and a consumer-only view. The listings reuse the shared
:class:`ListingEnvelope` so the Admin Panel consumes them with the same
pagination shape as every other directory.

SLA breach is a **derived** classification, never a stored column: a ticket is
breached when ``status != 'resolved' AND sla_due_at < now`` (R17.6). The flag is
computed at mapping time and surfaced as ``sla_breached`` on each ticket.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.support import SupportTicketStatus
from app.schemas.admin.listing import ListingEnvelope


class SupportMessageOut(BaseModel):
    """A single message in a ticket's history (R17.3)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ticket_id: uuid.UUID
    author_type: str
    author_id: uuid.UUID
    body: str
    created_at: datetime


class SupportTicketListItem(BaseModel):
    """A single row in the support ticket listing (R17.1).

    Carries the ticket ``id`` (identifier), ``subject``, the requester
    (``requester_type`` plus ``requester_id``), ``status``, ``priority``, the
    ``sla_due_at`` SLA due time, and the ``created_at`` creation date. The
    ``sla_breached`` flag is a derived classification (R17.6): it is True when
    the ticket is unresolved and its SLA due time is in the past.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reference: str
    subject: str
    requester_type: str
    requester_id: uuid.UUID
    status: str
    priority: str
    sla_due_at: datetime | None = None
    created_at: datetime
    sla_breached: bool = False


class SupportTicketDetail(SupportTicketListItem):
    """Detail record for a single ticket including its message history (R17.3)."""

    updated_at: datetime
    reason: str | None = None
    lead_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    messages: list[SupportMessageOut] = Field(default_factory=list)


#: Pagination envelope for ``GET /admin/support/tickets`` and the consumer view.
SupportTicketListResponse = ListingEnvelope[SupportTicketListItem]


class SupportMessageCreateRequest(BaseModel):
    """Payload for ``POST /admin/support/tickets/{id}/messages`` (R17.4).

    A non-empty ``body`` is required; an empty/whitespace body is rejected with
    HTTP 422.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    body: str = Field(min_length=1, max_length=4000)
    client_message_id: str | None = Field(default=None, min_length=16, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class SupportTicketStatusUpdateRequest(BaseModel):
    """Payload for ``PATCH /admin/support/tickets/{id}/status`` (R17.5).

    ``status`` must be a valid :class:`SupportTicketStatus` value
    (``open|pending|resolved``); any other value is rejected with HTTP 422.
    """

    status: SupportTicketStatus
