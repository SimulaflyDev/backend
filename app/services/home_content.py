import base64
import binascii
import io
import warnings

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.home_content import HomeCarousel
from app.schemas.home_content import CarouselOut, CarouselUpdate, default_carousel


async def get_carousel(db: AsyncSession, *, public: bool = False) -> CarouselOut:
    row = await db.get(HomeCarousel, 1)
    result = CarouselOut(version=row.version, **row.settings) if row else default_carousel()
    if public:
        result.slides = [slide for slide in result.slides if slide.enabled] if result.enabled else []
    return result


async def update_carousel(db: AsyncSession, payload: CarouselUpdate) -> CarouselOut:
    settings = payload.model_dump(exclude={"version"}, mode="json")
    next_version = payload.version + 1
    if payload.version == 0:
        db.add(HomeCarousel(id=1, version=next_version, settings=settings))
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "Carousel changed in another session. Reload before saving.") from None
    else:
        result = await db.execute(update(HomeCarousel).where(
            HomeCarousel.id == 1, HomeCarousel.version == payload.version,
        ).values(version=next_version, settings=settings))
        if result.rowcount != 1:
            raise HTTPException(409, "Carousel changed in another session. Reload before saving.")
    return CarouselOut(version=next_version, **settings)


def prepare_carousel_image(encoded: str) -> bytes:
    """Validate pixels and re-encode; never serve uploaded HTML/SVG or metadata."""
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 5 * 1024 * 1024:
            raise HTTPException(413, "Image must be 5 MB or smaller")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("Unsupported image")
                if image.width * image.height > 20_000_000:
                    raise ValueError("Image dimensions are too large")
                image.load()
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                normalized.thumbnail((1920, 1920))
                output = io.BytesIO()
                normalized.save(output, format="JPEG", quality=88, optimize=True)
                return output.getvalue()
    except HTTPException:
        raise
    except (ValueError, binascii.Error, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, "Upload a valid JPEG, PNG or WebP image (maximum 20 megapixels)") from None
