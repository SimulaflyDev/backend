import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.schemas.upload import (
    MerchantProductImageOut,
    MerchantProductImageUploadRequest,
    RoomImageOut,
    RoomImageUploadRequest,
)
from app.services.image_service import get_image, get_owned, persist_base64
from app.utils.dependencies import CurrentUser, DBSession
from app.utils.merchant_context import VerifiedMerchantContext

router = APIRouter(prefix="/upload", tags=["upload"])
settings = get_settings()


@router.post("/room-image", response_model=RoomImageOut, status_code=status.HTTP_201_CREATED)
@limiter.limit(f"{settings.UPLOAD_RATE_LIMIT_PER_HOUR}/hour")
async def upload_room_image(
    request: Request,
    response: Response,
    body: RoomImageUploadRequest,
    user: CurrentUser,
    db: DBSession,
) -> RoomImageOut:
    image = await persist_base64(
        db,
        owner_id=user.id,
        image_base64=body.image_base64,
        media_type=body.media_type,
        source="upload",
    )
    await db.commit()
    await db.refresh(image)
    return RoomImageOut(
        id=image.id,
        byte_size=image.byte_size,
        media_type=image.media_type,
        source=image.source,
        created_at=image.created_at,
    )


@router.get("/room-image/{image_id}")
async def fetch_room_image(image_id: uuid.UUID, db: DBSession) -> Response:
    image = await get_image(db, image_id=image_id)
    if not image:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="image not found")
    return Response(
        content=image.data,
        media_type=image.media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/room-image/{image_id}/download")
async def download_room_image(image_id: uuid.UUID, user: CurrentUser, db: DBSession) -> Response:
    """Private export of the original bytes; never publish a new share URL."""
    image = await get_owned(db, image_id=image_id, owner_id=user.id)
    if not image:
        raise HTTPException(status_code=404, detail="image not found")
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(image.media_type, "bin")
    return Response(content=image.data, media_type=image.media_type, headers={
        "Cache-Control": "private, no-store",
        "Content-Disposition": f'attachment; filename="Simulafly-{image.id}.{extension}"',
        "X-Content-Type-Options": "nosniff",
    })


@router.post(
    "/merchant-product-image",
    response_model=MerchantProductImageOut,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(f"{settings.UPLOAD_RATE_LIMIT_PER_HOUR}/hour")
async def upload_merchant_product_image(
    request: Request,
    response: Response,
    body: MerchantProductImageUploadRequest,
    ctx: VerifiedMerchantContext,
    db: DBSession,
) -> MerchantProductImageOut:
    image = await persist_base64(
        db,
        owner_id=ctx.member.user_id,
        image_base64=body.image_base64,
        media_type=body.media_type,
        source="merchant_product_images",
    )
    await db.commit()
    await db.refresh(image)
    url = f"/api/v1/upload/room-image/{image.id}"
    return MerchantProductImageOut(
        id=image.id,
        url=url,
        byte_size=image.byte_size,
        media_type=image.media_type,
        source=image.source,
        created_at=image.created_at,
    )
