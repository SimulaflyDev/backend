import asyncio
from sqlalchemy import text

from app.core.database import engine

async def main():
    async with engine.connect() as conn:
        res = await conn.execute(text("UPDATE users SET is_active = True;"))
        await conn.commit()
        print("Updated all users to is_active=True")

if __name__ == "__main__":
    asyncio.run(main())
