"""Admin support router — support center listing/detail/respond/status (R17).

Endpoints (all prefixed ``/admin``; mounted under ``/api/v1`` by the wiring
step):

| Method | Path                               | Permission        | Req            |
|--------|------------------------------------|-------------------|----------------|
| GET    | `/support/tickets`                 | `support.read`    | 17.1, 17.2     |
| GET    | `/support/consumer`                | `support.read`    | 17.7           |
| GET    | `/support/tickets/{id}`            | `support.read`    | 17.3, 17.8     |
| POST   | `/support/tickets/{id}/messages`   | `support.respond` | 17.4           |
| PATCH  | `/support/tickets/{id}/status`     | `support.respond` | 17.5           |

Reads are gated by ``require_permission("support.read")`` and the respond /
status-change actions by ``require_permission("support.respond")``. The mutating
routes commit the action and its immutable audit entry in the same transaction
(R17.4, R17.5 / R19.1).

SLA breach is a derived classification surfaced as ``sla_breached`` on every
ticket (``status != 'resolved' AND sla_due_at < now``, R17.6); see
:func:`app.services.admin.support_service.is_sla_breached`.

The static ``/support/consumer`` route is declared before the dynamic
``/support/tickets/{id}`` route so the literal path is always matched first.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.admin import AdminAccount
from app.models.support import (
    SupportRequesterType,
    SupportTicket,
    SupportTicketPriority,
    SupportTicketStatus,
)
from app.schemas.admin.support import (
    SupportMessageCreateRequest,
    SupportMessageOut,
    SupportTicketDetail,
    SupportTicketListItem,
    SupportTicketListResponse,
    SupportTicketStatusUpdateRequest,
)
from app.services.admin.audit_service import AuditService
from app.schemas.consumer_support import HelpSettings
from app.services.support_settings import get_help_settings, save_help_settings
from app.services.admin.listing import ListParams
from app.services.admin.support_service import SupportService, is_sla_breached
from app.utils.admin_dependencies import require_permission

router = APIRouter(prefix="/admin", tags=["admin-support"])

DBSession = Annotated[AsyncSession, Depends(get_db)]


def _to_list_item(ticket: SupportTicket) -> SupportTicketListItem:
    return SupportTicketListItem(
        id=ticket.id,
        reference=ticket.reference,
        subject=ticket.subject,
        requester_type=ticket.requester_type,
        requester_id=ticket.requester_id,
        status=ticket.status,
        priority=ticket.priority,
        sla_due_at=ticket.sla_due_at,
        created_at=ticket.created_at,
        sla_breached=is_sla_breached(ticket),
    )


def _to_detail(ticket: SupportTicket) -> SupportTicketDetail:
    return SupportTicketDetail(
        id=ticket.id,
        reference=ticket.reference,
        subject=ticket.subject,
        requester_type=ticket.requester_type,
        requester_id=ticket.requester_id,
        status=ticket.status,
        priority=ticket.priority,
        sla_due_at=ticket.sla_due_at,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        reason=ticket.reason,
        lead_id=ticket.lead_id,
        session_id=ticket.session_id,
        sla_breached=is_sla_breached(ticket),
        messages=[SupportMessageOut.model_validate(m) for m in ticket.messages],
    )


def _envelope(page_obj) -> SupportTicketListResponse:
    return SupportTicketListResponse(
        items=[_to_list_item(ticket) for ticket in page_obj.items],
        page=page_obj.page,
        page_size=page_obj.page_size,
        total=page_obj.total,
        total_pages=page_obj.total_pages,
        has_next=page_obj.has_next,
        next_page=page_obj.next_page,
    )


@router.get(
    "/support/tickets",
    response_model=SupportTicketListResponse,
    dependencies=[Depends(require_permission("support.read"))],
)
async def list_tickets(
    db: DBSession,
    search: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    status: SupportTicketStatus | None = Query(default=None),
    priority: SupportTicketPriority | None = Query(default=None),
    requester_type: SupportRequesterType | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1),
) -> SupportTicketListResponse:
    """Paginated support tickets with conjunctive status/priority/requester filters.

    Searchable by subject (R17.1) and filterable by status, priority, and
    requester type (R17.2); an unsupported sort field is rejected with HTTP 422.
    """
    params = ListParams(page=page, search=search, sort=sort)
    if page_size is not None:
        params.page_size = page_size

    page_obj = await SupportService(db).list_tickets(
        params,
        status=status.value if status is not None else None,
        priority=priority.value if priority is not None else None,
        requester_type=requester_type.value if requester_type is not None else None,
    )
    return _envelope(page_obj)


@router.get(
    "/support/consumer",
    response_model=SupportTicketListResponse,
    dependencies=[Depends(require_permission("support.read"))],
)
async def list_consumer_tickets(
    db: DBSession,
    search: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    status: SupportTicketStatus | None = Query(default=None),
    priority: SupportTicketPriority | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1),
) -> SupportTicketListResponse:
    """Paginated consumer-originated tickets only (``requester_type='consumer'``, R17.7)."""
    params = ListParams(page=page, search=search, sort=sort)
    if page_size is not None:
        params.page_size = page_size

    service = SupportService(db)
    page_obj = await service.list_tickets(
        params,
        status=status.value if status is not None else None,
        priority=priority.value if priority is not None else None,
        requester_type=SupportRequesterType.CONSUMER.value,
    )
    return _envelope(page_obj)


@router.get(
    "/support/tickets/{ticket_id}",
    response_model=SupportTicketDetail,
    dependencies=[Depends(require_permission("support.read"))],
)
async def get_ticket(
    ticket_id: uuid.UUID,
    db: DBSession,
) -> SupportTicketDetail:
    """Return a single ticket's detail with full message history; 404 if missing (R17.3, R17.8)."""
    ticket = await SupportService(db).get_ticket(ticket_id)
    return _to_detail(ticket)


@router.post(
    "/support/tickets/{ticket_id}/messages",
    response_model=SupportTicketDetail,
)
async def respond_to_ticket(
    ticket_id: uuid.UUID,
    payload: SupportMessageCreateRequest,
    db: DBSession,
    admin: Annotated[AdminAccount, Depends(require_permission("support.respond"))],
) -> SupportTicketDetail:
    """Append the admin's response to the ticket history and record it (R17.4)."""
    ticket = await SupportService(db).respond(
        ticket_id,
        author_id=admin.id,
        body=payload.body,
        client_message_id=payload.client_message_id,
    )
    await AuditService(db).record(actor=admin, action="support.respond", target_type="support_ticket",
        target_id=ticket_id, metadata={"message_count": len(ticket.messages)})
    await db.commit()
    return _to_detail(ticket)


@router.patch(
    "/support/tickets/{ticket_id}/status",
    response_model=SupportTicketDetail,
)
async def update_ticket_status(
    ticket_id: uuid.UUID,
    payload: SupportTicketStatusUpdateRequest,
    db: DBSession,
    admin: Annotated[AdminAccount, Depends(require_permission("support.respond"))],
) -> SupportTicketDetail:
    """Persist a new ticket status and record the action (R17.5)."""
    ticket = await SupportService(db).change_status(ticket_id, payload.status)
    await AuditService(db).record(actor=admin, action="support.status", target_type="support_ticket",
        target_id=ticket_id, metadata={"status": ticket.status})
    await db.commit()
    return _to_detail(ticket)


@router.get("/support/settings", response_model=HelpSettings,
    dependencies=[Depends(require_permission("content.read"))])
async def read_settings(db: DBSession):
    return await get_help_settings(db)


@router.put("/support/settings", response_model=HelpSettings)
async def update_settings(body: HelpSettings, db: DBSession,
    admin: Annotated[AdminAccount, Depends(require_permission("content.manage"))]):
    result = await save_help_settings(db, body)
    await AuditService(db).record(actor=admin, action="support.settings.update", target_type="support_settings",
        target_id=1, metadata={"version": result.version})
    await db.commit()
    return result
