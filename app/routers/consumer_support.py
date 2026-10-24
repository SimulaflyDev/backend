import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.models.lead import BuyerLead
from app.models.session import DesignSession
from app.models.support import SupportTicket, SupportMessage, new_support_reference
from app.models.user import User
from app.schemas.consumer_support import (
    CustomerMessageCreate, CustomerTicketCreate, CustomerTicketDetail,
    CustomerTicketPage, HelpSettings,
)
from app.services.support_settings import get_help_settings
from app.utils.dependencies import CurrentUser, DBSession

router = APIRouter(prefix="/support", tags=["customer-support"])


async def owned_ticket(db, ticket_id, user_id, *, lock=False):
    statement = select(SupportTicket).where(
        SupportTicket.id == ticket_id, SupportTicket.requester_id == user_id,
        SupportTicket.requester_type == "consumer", SupportTicket.deleted_at.is_(None),
    ).options(selectinload(SupportTicket.messages)).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    ticket = (await db.execute(statement)).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(404, "Support ticket not found")
    return ticket


@router.get("/settings", response_model=HelpSettings)
async def help_settings(db: DBSession, response: Response):
    response.headers["Cache-Control"] = "no-cache"
    return await get_help_settings(db)


@router.get("/tickets", response_model=CustomerTicketPage)
async def list_tickets(db: DBSession, user: CurrentUser,
    limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0)):
    conditions = (
        SupportTicket.requester_id == user.id, SupportTicket.requester_type == "consumer",
        SupportTicket.deleted_at.is_(None),
    )
    total = await db.scalar(select(func.count()).select_from(SupportTicket).where(*conditions))
    tickets = (await db.scalars(select(SupportTicket).where(*conditions).order_by(
        SupportTicket.updated_at.desc(), SupportTicket.id.desc(),
    ).offset(offset).limit(limit))).all()
    return {"items": tickets, "total": total, "limit": limit, "offset": offset}


@router.post("/tickets", response_model=CustomerTicketDetail, status_code=201)
async def create_ticket(body: CustomerTicketCreate, db: DBSession, user: CurrentUser):
    # Serialize submissions per customer, including retries arriving concurrently.
    await db.scalar(select(User.id).where(User.id == user.id).with_for_update())
    existing = await db.scalar(select(SupportTicket).where(
        SupportTicket.requester_id == user.id, SupportTicket.requester_type == "consumer",
        SupportTicket.client_request_id == body.client_request_id,
    ))
    if existing is not None:
        if (existing.subject, existing.description, existing.reason, existing.lead_id, existing.session_id) != (
            body.subject, body.description, body.category, body.lead_id, body.session_id,
        ):
            raise HTTPException(409, "This request ID already belongs to a different ticket")
        return await owned_ticket(db, existing.id, user.id)
    if not (await get_help_settings(db)).accepting_tickets:
        raise HTTPException(503, "New support tickets are temporarily paused. Please use the contact details on the support page.")
    if body.lead_id is not None:
        lead = await db.scalar(select(BuyerLead).where(BuyerLead.id == body.lead_id, BuyerLead.user_id == user.id))
        if lead is None:
            raise HTTPException(404, "Order not found")
    if body.session_id is not None:
        session = await db.scalar(select(DesignSession).where(DesignSession.id == body.session_id, DesignSession.user_id == user.id))
        if session is None:
            raise HTTPException(404, "Design session not found")
    active_count = await db.scalar(select(func.count()).select_from(SupportTicket).where(
        SupportTicket.requester_id == user.id, SupportTicket.requester_type == "consumer",
        SupportTicket.status != "resolved", SupportTicket.deleted_at.is_(None),
    ))
    if active_count >= 20:
        raise HTTPException(429, "You already have 20 active requests. Please reply to an existing ticket.")
    for _ in range(10):
        try:
            async with db.begin_nested():
                ticket = SupportTicket(
                    reference=new_support_reference(), subject=body.subject, reason=body.category,
                    requester_type="consumer", requester_id=user.id,
                    description=body.description, client_request_id=body.client_request_id,
                    lead_id=body.lead_id, session_id=body.session_id,
                )
                db.add(ticket)
                await db.flush()
                db.add(SupportMessage(ticket_id=ticket.id, author_type="requester",
                    author_id=user.id, body=body.description))
                await db.flush()
        except IntegrityError:
            # A reference collision rolls back only the attempted ticket.
            continue
        # Do not retry errors raised during the outer commit: the transaction
        # may already be committed (or require rollback after a connection loss).
        await db.commit()
        return await owned_ticket(db, ticket.id, user.id)
    raise HTTPException(503, "Could not allocate a ticket reference. Please retry.")


@router.get("/tickets/{ticket_id}", response_model=CustomerTicketDetail)
async def get_ticket(ticket_id: uuid.UUID, db: DBSession, user: CurrentUser):
    return await owned_ticket(db, ticket_id, user.id)


@router.post("/tickets/{ticket_id}/messages", response_model=CustomerTicketDetail)
async def reply(ticket_id: uuid.UUID, body: CustomerMessageCreate, db: DBSession, user: CurrentUser):
    ticket = await owned_ticket(db, ticket_id, user.id, lock=True)
    existing = await db.scalar(select(SupportMessage).where(
        SupportMessage.ticket_id == ticket.id, SupportMessage.client_message_id == body.client_message_id,
    ))
    if existing is not None:
        if existing.author_id != user.id or existing.author_type != "requester" or existing.body != body.body:
            raise HTTPException(409, "Message request ID has already been used")
        return ticket
    db.add(SupportMessage(ticket_id=ticket.id, author_type="requester", author_id=user.id,
        body=body.body, client_message_id=body.client_message_id))
    ticket.updated_at = datetime.now(timezone.utc)
    ticket.status = "open"  # A follow-up reopens a resolved request.
    await db.commit()
    return await owned_ticket(db, ticket_id, user.id)
