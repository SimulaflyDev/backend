"""Support center service (R17 — support tickets).

``SupportService`` backs the admin ``support`` router. It reuses the shared
listing engine (:func:`app.services.admin.listing.paginate`) so search, the
status/priority/requester-type filters, sort, soft-delete exclusion, and
pagination behave identically to every other admin directory:

  * **List** tickets (paginated) — filterable by ``status``, ``priority``, and
    ``requester_type`` (conjunctively, R17.1, R17.2), sortable by a whitelist,
    excluding soft-deleted rows (R20/R22).
  * **Detail** — fetch a single ticket with its full message history eager
    loaded; a missing identifier raises HTTP 404 (R17.3, R17.8).
  * **Respond** — append exactly one :class:`SupportMessage` to the ticket's
    history and return the reloaded ticket (R17.4). The router wraps this in
    ``audited(...)`` so the action is recorded.
  * **Change status** — persist a new :class:`SupportTicketStatus` value
    (R17.5); also audited at the router boundary.
  * **Consumer view** — the same listing constrained to
    ``requester_type='consumer'`` (R17.7).

SLA breach is a **derived** classification, never stored: a ticket is breached
when ``status != 'resolved' AND sla_due_at < now`` (R17.6). :func:`is_sla_breached`
is the single shared predicate the router uses when mapping tickets to schemas.

This service owns only persistence-level logic and its invariants; the router
handles permission gating, auditing, and schema mapping.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.models.support import (
    SupportMessage,
    SupportMessageAuthorType,
    SupportRequesterType,
    SupportTicket,
    SupportTicketStatus,
)
from app.services.admin.listing import ListParams, Page, paginate


def _now() -> datetime:
    """Current UTC instant (timezone-aware) used for SLA-breach evaluation."""
    return datetime.now(timezone.utc)


def is_sla_breached(ticket: SupportTicket, *, now: datetime | None = None) -> bool:
    """Derive whether a ticket is in SLA breach (R17.6).

    A ticket is breached when it is **not resolved** and its ``sla_due_at`` is
    set and lies strictly in the past. The classification is computed on demand
    and never persisted.
    """
    if ticket.sla_due_at is None:
        return False
    if ticket.status == SupportTicketStatus.RESOLVED.value:
        return False
    reference = now or _now()
    due = ticket.sla_due_at
    # Treat a naive stored timestamp as UTC so the comparison is well-defined.
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due < reference


class SupportService:
    """List / detail / respond / status / consumer operations for support (R17)."""

    #: Whitelisted sortable (and string-filterable) fields → column expressions.
    SORTABLE: dict[str, ColumnElement] = {
        "reference": SupportTicket.reference,
        "subject": SupportTicket.subject,
        "status": SupportTicket.status,
        "priority": SupportTicket.priority,
        "requester_type": SupportTicket.requester_type,
        "sla_due_at": SupportTicket.sla_due_at,
        "created_at": SupportTicket.created_at,
    }

    #: Columns OR-matched (ILIKE) against the free-text search term (R17.1).
    SEARCHABLE: list[ColumnElement] = [SupportTicket.reference, SupportTicket.subject]

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_tickets(
        self,
        params: ListParams,
        *,
        status: str | None = None,
        priority: str | None = None,
        requester_type: str | None = None,
    ) -> Page:
        """Return a paginated page of support tickets (R17.1, R17.2).

        ``status``, ``priority``, and ``requester_type`` are applied as
        conjunctive equality filters when provided. Soft-deleted rows are
        excluded by default.
        """
        if status is not None:
            params.filters[SupportTicket.status] = status
        if priority is not None:
            params.filters[SupportTicket.priority] = priority
        if requester_type is not None:
            params.filters[SupportTicket.requester_type] = requester_type

        base_stmt = select(SupportTicket)
        return await paginate(
            self.db,
            base_stmt,
            params=params,
            sortable=self.SORTABLE,
            searchable=self.SEARCHABLE,
            soft_delete_col=SupportTicket.deleted_at,
        )

    async def list_consumer_tickets(self, params: ListParams) -> Page:
        """Return a paginated page of consumer-originated tickets only (R17.7)."""
        return await self.list_tickets(
            params,
            requester_type=SupportRequesterType.CONSUMER.value,
        )

    async def get_ticket(self, ticket_id: uuid.UUID, *, lock: bool = False) -> SupportTicket:
        """Fetch a single non-deleted ticket with its message history loaded.

        The ``messages`` relationship is eager-loaded (ordered by creation time)
        so the router can expose the full history. Raises HTTP 404 when no
        matching ticket exists (R17.8).
        """
        stmt = (
            select(SupportTicket)
            .options(selectinload(SupportTicket.messages))
            .where(SupportTicket.id == ticket_id, SupportTicket.deleted_at.is_(None))
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        ticket = (await self.db.execute(stmt)).scalar_one_or_none()
        if ticket is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="support ticket not found",
            )
        return ticket

    async def respond(
        self,
        ticket_id: uuid.UUID,
        *,
        author_id: uuid.UUID,
        body: str,
        client_message_id: str | None = None,
    ) -> SupportTicket:
        """Append exactly one admin message to the ticket history (R17.4).

        Adds a single :class:`SupportMessage` authored by the acting admin and
        returns the reloaded ticket with its full (now +1) history. A missing
        ticket raises HTTP 404 (R17.8).
        """
        ticket = await self.get_ticket(ticket_id, lock=True)
        if client_message_id is not None:
            existing = await self.db.scalar(select(SupportMessage).where(
                SupportMessage.ticket_id == ticket.id,
                SupportMessage.client_message_id == client_message_id,
            ))
            if existing is not None:
                if existing.body != body or existing.author_id != author_id or existing.author_type != "admin":
                    raise HTTPException(409, "Message request ID has already been used")
                return ticket
        message = SupportMessage(
            ticket_id=ticket.id,
            author_type=SupportMessageAuthorType.ADMIN.value,
            author_id=author_id,
            body=body,
            client_message_id=client_message_id,
        )
        self.db.add(message)
        ticket.updated_at = _now()
        ticket.status = SupportTicketStatus.PENDING.value
        await self._notify(ticket, "Support replied", f"New reply to #{ticket.reference}: {ticket.subject}")
        await self.db.flush()
        # Reload the ticket with the appended message included in history.
        return await self.get_ticket(ticket_id)

    async def change_status(
        self,
        ticket_id: uuid.UUID,
        new_status: SupportTicketStatus,
    ) -> SupportTicket:
        """Persist a new status for the ticket and return it (R17.5).

        A missing ticket raises HTTP 404 (R17.8).
        """
        ticket = await self.get_ticket(ticket_id, lock=True)
        if ticket.status == new_status.value:
            return ticket
        ticket.status = new_status.value
        ticket.updated_at = _now()
        await self._notify(ticket, "Support request updated", f"Request #{ticket.reference} is now {ticket.status}.")
        await self.db.flush()
        return await self.get_ticket(ticket_id)

    async def _notify(self, ticket: SupportTicket, title: str, summary: str) -> None:
        from app.models.merchant import MerchantMember
        from app.models.notification import Notification
        from app.models.user import User

        if ticket.requester_type == SupportRequesterType.CONSUMER.value:
            recipients = [ticket.requester_id] if await self.db.get(User, ticket.requester_id) else []
        else:
            recipients = list((await self.db.scalars(select(MerchantMember.user_id).where(
                MerchantMember.merchant_id == ticket.requester_id,
            ))).all())
        for user_id in set(recipients):
            self.db.add(Notification(user_id=user_id, kind="system", title=title, summary=summary,
                payload={"ticket_id": str(ticket.id), "requester_type": ticket.requester_type,
                         "support_status": ticket.status}))
