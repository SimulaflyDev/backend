from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.models.support import SupportSettings
from app.schemas.consumer_support import HelpSettings


async def get_help_settings(db) -> HelpSettings:
    row = await db.get(SupportSettings, 1)
    return HelpSettings(version=row.version, **row.settings) if row else HelpSettings()


async def save_help_settings(db, payload: HelpSettings) -> HelpSettings:
    values = payload.model_dump(exclude={"version"}, mode="json")
    next_version = payload.version + 1
    if payload.version == 0:
        try:
            async with db.begin_nested():
                db.add(SupportSettings(id=1, version=next_version, settings=values))
                await db.flush()
        except IntegrityError:
            raise HTTPException(409, "Support settings changed. Reload before saving.") from None
    else:
        result = await db.execute(update(SupportSettings).where(
            SupportSettings.id == 1, SupportSettings.version == payload.version,
        ).values(version=next_version, settings=values))
        if result.rowcount != 1:
            raise HTTPException(409, "Support settings changed. Reload before saving.")
    return HelpSettings(version=next_version, **values)
