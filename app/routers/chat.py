import asyncio
import base64
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response, status
from langchain_core.messages import HumanMessage
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.logging import get_logger
from app.core.rate_limit import limiter
from app.models.message import Message
from app.models.session import DesignSession
from app.schemas.chat import ChatAnalyzeRequest, ChatRequest, ChatResponse, MessageOut
from app.schemas.product import MerchantProductOut
from app.services.azure_ai_client import get_image_client
from app.services.image_service import persist_base64, persist_image
from app.services.llm import get_chat_llm
from app.services.user_tokens import debit_tokens
from app.services.rag_service import run_rag_turn
from app.services.user_profile_service import extract_and_update_profile
from app.services.visualize_jobs import (
    create_job,
    mark_done,
    mark_failed,
)
from app.utils.dependencies import CurrentUser, DBSession

log = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])
settings = get_settings()


async def _owned_session(db, session_id: uuid.UUID, user_id: uuid.UUID) -> DesignSession:
    res = await db.execute(
        select(DesignSession).where(
            DesignSession.id == session_id, DesignSession.user_id == user_id
        )
    )
    session = res.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    return session


@router.post("/analyze", response_model=ChatResponse)
@limiter.limit(f"{settings.CHAT_RATE_LIMIT_PER_MINUTE}/minute")
async def analyze(
    request: Request,
    response: Response,
    body: ChatAnalyzeRequest,
    user: CurrentUser,
    db: DBSession,
) -> ChatResponse:
    session = await _owned_session(db, body.session_id, user.id)

    # Check and deduct credits for makeover if style_name is present
    if body.style_name:
        cost = 2.0
        await debit_tokens(db, user, cost)

    image = await persist_base64(
        db,
        owner_id=user.id,
        image_base64=body.image_base64,
        media_type=body.media_type,
        source="upload",
    )
    if not session.room_image_id:
        session.room_image_id = image.id

    style_directive = ""
    if body.style_name:
        vibe = f" — {body.style_vibe}" if body.style_vibe else ""
        style_directive = (
            f"\n\nThe user has selected the '{body.style_name}' aesthetic{vibe} "
            "as the design direction. Ground your description and follow-up "
            "in this style: which existing pieces work with it, which gaps the "
            "style would let you fill, and what one decision the user should "
            "make first to commit to it."
        )
        # Persist on the session so /chat/ turns can reference it.
        if hasattr(session, "profile_snapshot"):
            snap = dict(session.profile_snapshot or {})
            snap["selected_style"] = {
                "slug": body.style_slug,
                "name": body.style_name,
                "vibe": body.style_vibe,
            }
            session.profile_snapshot = snap

    prompt = (
        "You are Sumi, an interior designer. Describe this room in 2-3 sentences "
        "(style, lighting, key pieces, empty space). Then ask the user ONE concrete "
        "clarifying question about what they want to add or change. Keep it warm and brief."
        + style_directive
    )
    image_b64 = base64.b64encode(image.data).decode()
    vision_message = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image.media_type};base64,{image_b64}"},
            },
        ]
    )
    llm = get_chat_llm(temperature=0.4, max_tokens=512)
    response = await llm.ainvoke([vision_message])
    reply = response.content if isinstance(response.content, str) else str(response.content)

    session.context_summary = reply
    # Surface the chosen style as the user's first turn — the chat UI shows
    # this above the room photo as a small chip ("Style: Mid-Century Modern").
    user_content = (
        f"Style: {body.style_name}" if body.style_name else "[room scan attached]"
    )
    user_msg = Message(session_id=session.id, role="user", content=user_content, image_id=image.id)
    initial_suggestions = [
        "What should I change first?",
        "Suggest a colour palette",
        "Show me furniture options",
    ]
    assistant_msg = Message(
        session_id=session.id,
        role="assistant",
        content=reply,
        ui_payload={"type": "suggestions", "suggestions": initial_suggestions},
    )
    db.add_all([user_msg, assistant_msg])
    await db.commit()
    await db.refresh(assistant_msg)

    # If the user picked a style, kick off a background image-edit job that
    # produces a styled makeover of their actual room photo. The result
    # lands as a `room_preview` assistant message in this session; the
    # client polls /visualize/{task_id} to know when to refresh.
    makeover_task_id: uuid.UUID | None = None
    if body.style_name:
        job = await create_job(
            db,
            user_id=user.id,
            session_id=session.id,
            product_id=None,
            room_image_id=image.id,
        )
        await db.commit()
        makeover_task_id = job.id
        asyncio.create_task(
            _run_style_makeover(
                job_id=job.id,
                user_id=user.id,
                session_id=session.id,
                room_bytes=image.data,
                style_name=body.style_name,
                style_vibe=body.style_vibe,
            )
        )

    return ChatResponse(
        message_id=assistant_msg.id,
        content=assistant_msg.content,
        ui_payload=assistant_msg.ui_payload,
        created_at=assistant_msg.created_at,
        makeover_task_id=makeover_task_id,
    )


def _style_makeover_prompt(style_name: str, style_vibe: str | None) -> str:
    """Builds a strict-room-makeover prompt for the first-turn auto-render.
    Uses the picked style as the sole stylistic input — no chat history is
    blended in (that's what produced muddy results in the prior makeover
    iteration)."""
    vibe_line = f" Vibe: {style_vibe.strip()}." if style_vibe else ""
    return (
        "You are a photorealistic interior-design compositor. "
        "Restyle the provided room photo into a complete makeover.\n"
        f"Restyling direction: '{style_name}'.{vibe_line}\n"
        "Rules you MUST follow:\n"
        "  1. Preserve the room's overall geometry, perspective, camera "
        "angle, and the position/size of architectural features "
        "(walls, windows, doors, ceiling height).\n"
        "  2. You MAY change furniture, decor, colour palette, textiles, "
        "lighting, and accessories to realise the direction above.\n"
        "  3. The result must look like a real photograph — accurate "
        "lighting, contact shadows, and material reflections.\n"
        "Output: one photorealistic interior photograph, no text overlays, "
        "no watermarks."
    )[:3800]


async def _run_style_makeover(
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    room_bytes: bytes,
    style_name: str,
    style_vibe: str | None,
) -> None:
    """Background worker: image-edit the room photo into the chosen style
    and persist the result as a `room_preview` assistant message in the
    session. Mirrors the single-product visualize flow, just product-less
    and style-driven."""
    try:
        ai = get_image_client()
        prompt = _style_makeover_prompt(style_name, style_vibe)
        log.info(
            "analyze.makeover.start",
            job_id=str(job_id),
            session_id=str(session_id),
            style=style_name,
        )
        png_bytes = await ai.image_edit(
            room_bytes, None, prompt, fallback_prompt=prompt
        )
        async with SessionLocal() as db:
            generated = await persist_image(
                db,
                owner_id=user_id,
                data=png_bytes,
                media_type="image/png",
                source="generated_preview",
            )
            assistant_msg = Message(
                session_id=session_id,
                role="assistant",
                content=f"Here's your room reimagined in {style_name}.",
                ui_payload={
                    "type": "room_preview",
                    "image_id": str(generated.id),
                    "product_id": None,
                    "suggestions": [
                        "Try another design style",
                        "Change the colour palette",
                        "Find products for this look",
                    ],
                },
                image_id=generated.id,
            )
            db.add(assistant_msg)
            await db.flush()
            await mark_done(db, job_id, image_id=generated.id, message_id=assistant_msg.id)
            await db.commit()
            log.info(
                "analyze.makeover.done",
                job_id=str(job_id),
                image_id=str(generated.id),
                bytes=len(png_bytes),
            )
    except Exception as e:
        log.exception("analyze.makeover.failed", job_id=str(job_id), error=str(e))
        await mark_failed(job_id, str(e))


@router.post("/", response_model=ChatResponse)
@limiter.limit(f"{settings.CHAT_RATE_LIMIT_PER_MINUTE}/minute")
async def chat(
    request: Request,
    response: Response,
    body: ChatRequest,
    user: CurrentUser,
    db: DBSession,
    background: BackgroundTasks,
) -> ChatResponse:
    session = await _owned_session(db, body.session_id, user.id)

    # Validate: at least one of content or image must be provided.
    has_content = bool(body.content and body.content.strip())
    has_image = body.image_base64 is not None
    if not has_content and not has_image:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provide either text content or an image attachment",
        )

    # If an image is attached, persist it and store the resulting image id
    # on the user message so the chat history can render the photo.
    attached_image_id: uuid.UUID | None = None
    attached_image_bytes: bytes | None = None
    if has_image:
        image = await persist_base64(
            db,
            owner_id=user.id,
            image_base64=body.image_base64,  # type: ignore[arg-type]
            media_type=body.media_type,
            source="upload",
        )
        attached_image_id = image.id
        attached_image_bytes = image.data

    # Run the agent before adding this turn to the history query. This prevents
    # the current prompt being sent to the model twice (once as history and
    # once as the current HumanMessage).
    agent_text = body.content if has_content else "What can you tell me about this image?"
    image_data_url = None
    if has_image:
        clean_base64 = body.image_base64.split(",", 1)[-1]  # type: ignore[union-attr]
        image_data_url = f"data:{body.media_type};base64,{clean_base64}"
    result = await run_rag_turn(
        db,
        session_id=session.id,
        user_message=agent_text,
        context_summary=session.context_summary,
        design_profile=user.design_profile or {},
        image_data_url=image_data_url,
    )

    user_msg = Message(
        session_id=session.id,
        role="user",
        content=body.content,
        image_id=attached_image_id,
    )
    db.add(user_msg)

    # Phase 4: emit ai_rag_mention for each MerchantProduct the RAG surfaced.
    # Guard with isinstance — rag_service still returns legacy Product objects;
    # emission is a no-op until rag_service is migrated (Phase 4b).
    from app.models.merchant_product import MerchantProduct as _MerchantProduct
    from app.services.billing import BillingService as _BillingService
    _mp_products = [p for p in result.products if isinstance(p, _MerchantProduct)]
    if _mp_products:
        _billing = _BillingService(db)
        for _rank, _p in enumerate(_mp_products):
            try:
                await _billing.record_event(
                    event_type="ai_rag_mention",
                    user_id=user.id,
                    merchant_id=_p.merchant_id,
                    product_id=_p.id,
                    session_id=None,
                    context={
                        "prompt": (body.content or "")[:500],
                        "rank": _rank,
                    },
                )
            except Exception:
                log.warning("rag_emission_failed", product_id=str(_p.id))
        for _mid in {_p.merchant_id for _p in _mp_products}:
            background.add_task(_billing.pause_if_depleted_for, _mid)

    ui_payload = None
    if result.products:
        ui_payload = {
            "type": "product_carousel",
            "products": [MerchantProductOut.model_validate(p).model_dump(mode="json") for p in result.products],
        }
    elif result.preview_product_ids:
        # Multi-product composite preview (different categories selected together)
        ui_payload = {
            "type": "preview_request",
            "product_ids": [str(pid) for pid in result.preview_product_ids],
        }
    elif result.preview_product_id:
        # Single-product preview
        ui_payload = {"type": "preview_request", "product_id": str(result.preview_product_id)}

    suggestions = list(result.suggested_questions or [])[:3]
    if suggestions:
        if ui_payload is None:
            ui_payload = {"type": "suggestions"}
        ui_payload["suggestions"] = suggestions

    image_job = None
    if result.image_generation_prompt:
        image_cost = 2.0
        await debit_tokens(db, user, image_cost)
        image_job = await create_job(
            db,
            user_id=user.id,
            session_id=session.id,
            product_id=None,
            room_image_id=None,
        )

    assistant_msg = Message(
        session_id=session.id,
        role="assistant",
        content=result.assistant_text,
        ui_payload=ui_payload,
    )
    db.add(assistant_msg)
    session.updated_at = func.now()
    await db.commit()
    await db.refresh(assistant_msg)

    if image_job is not None:
        asyncio.create_task(
            _run_prompt_image_generation(
                job_id=image_job.id,
                user_id=user.id,
                session_id=session.id,
                prompt=result.image_generation_prompt or body.content,
                reference_image=attached_image_bytes,
            )
        )

    background.add_task(extract_and_update_profile, user.id, session.id)

    return ChatResponse(
        message_id=assistant_msg.id,
        content=assistant_msg.content,
        ui_payload=assistant_msg.ui_payload,
        created_at=assistant_msg.created_at,
        image_task_id=image_job.id if image_job else None,
    )


async def _run_prompt_image_generation(
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    prompt: str,
    reference_image: bytes | None = None,
) -> None:
    """Generate an arbitrary image and append it to the same conversation."""
    try:
        log.info("chat.image_generation.start", job_id=str(job_id), session_id=str(session_id))
        ai = get_image_client()
        if reference_image is not None:
            png_bytes = await ai.image_edit(
                reference_image,
                None,
                prompt,
                fallback_prompt=prompt,
            )
        else:
            png_bytes = await ai.image_gen(prompt, seed=None)
        async with SessionLocal() as db:
            generated = await persist_image(
                db,
                owner_id=user_id,
                data=png_bytes,
                media_type="image/png",
                source="generated_ai",
            )
            assistant_msg = Message(
                session_id=session_id,
                role="assistant",
                content="Here’s the image I created for you.",
                ui_payload={
                    "type": "room_preview",
                    "image_id": str(generated.id),
                    "product_id": None,
                    "suggestions": [
                        "Create a different style",
                        "Change a detail in this image",
                        "Generate another variation",
                    ],
                },
                image_id=generated.id,
            )
            db.add(assistant_msg)
            await db.flush()
            await mark_done(db, job_id, image_id=generated.id, message_id=assistant_msg.id)
            await db.commit()
            log.info(
                "chat.image_generation.done",
                job_id=str(job_id),
                image_id=str(generated.id),
                bytes=len(png_bytes),
            )
    except Exception as e:
        log.exception("chat.image_generation.failed", job_id=str(job_id), error=str(e))
        await mark_failed(job_id, str(e))


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(
    session_id: uuid.UUID, user: CurrentUser, db: DBSession
) -> list[Message]:
    await _owned_session(db, session_id, user.id)
    res = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.created_at.asc())
    )
    return list(res.scalars().all())
