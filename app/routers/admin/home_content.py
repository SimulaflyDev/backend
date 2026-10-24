from typing import Annotated

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.models.admin import AdminAccount
from app.models.home_content import HomeCarouselImage
from app.schemas.home_content import CarouselImageOut, CarouselImageUpload, CarouselOut, CarouselUpdate
from app.services.admin.audit_service import AuditService
from app.services.home_content import get_carousel, prepare_carousel_image, update_carousel
from app.utils.admin_dependencies import require_permission
from app.utils.dependencies import DBSession

router = APIRouter(prefix="/admin/home/carousel", tags=["admin-home"])
Editor = Annotated[AdminAccount, Depends(require_permission("content.manage"))]


@router.get("", response_model=CarouselOut, dependencies=[Depends(require_permission("content.read"))])
async def read_carousel(db: DBSession):
    return await get_carousel(db)


@router.put("", response_model=CarouselOut)
async def save_carousel(body: CarouselUpdate, db: DBSession, actor: Editor):
    result = await update_carousel(db, body)
    await AuditService(db).record(actor=actor, action="home.carousel.update", target_type="home_carousel", target_id=1,
        metadata={"version": result.version, "enabled": result.enabled, "slide_ids": [s.id for s in result.slides]})
    await db.commit()
    return result


@router.post("/images", response_model=CarouselImageOut, status_code=201)
async def upload_image(body: CarouselImageUpload, db: DBSession, actor: Editor):
    data = await run_in_threadpool(prepare_carousel_image, body.image_base64)
    image = HomeCarouselImage(data=data, media_type="image/jpeg")
    db.add(image)
    await db.flush()
    image_id = image.id
    await AuditService(db).record(actor=actor, action="home.carousel.image.upload", target_type="home_carousel_image", target_id=image_id,
        metadata={"byte_size": len(data)})
    await db.commit()
    return CarouselImageOut(url=f"/api/v1/home/carousel/images/{image_id}", byte_size=len(data))
