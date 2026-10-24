import uuid

from fastapi import APIRouter, HTTPException, Response

from app.models.home_content import HomeCarouselImage
from app.schemas.home_content import CarouselOut
from app.services.home_content import get_carousel
from app.utils.dependencies import DBSession

router = APIRouter(prefix="/home/carousel", tags=["home"])


@router.get("", response_model=CarouselOut)
async def public_carousel(db: DBSession, response: Response):
    response.headers["Cache-Control"] = "no-cache"
    return await get_carousel(db, public=True)


@router.get("/images/{image_id}")
async def carousel_image(image_id: uuid.UUID, db: DBSession):
    image = await db.get(HomeCarouselImage, image_id)
    if image is None:
        raise HTTPException(404, "Carousel image not found")
    return Response(content=image.data, media_type=image.media_type, headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    })
