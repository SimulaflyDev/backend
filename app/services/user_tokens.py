"""Atomic token updates so chat spending cannot overwrite an order reward."""
import math

from fastapi import HTTPException
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.user import User


async def debit_tokens(db: AsyncSession, user: User, amount: float) -> None:
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Token cost must be positive and finite")
    result = await db.execute(update(User).where(
        User.id == user.id, User.credit_balance >= amount,
    ).values(credit_balance=User.credit_balance - amount).returning(User.credit_balance))
    balance = result.scalar_one_or_none()
    if balance is None:
        raise HTTPException(402, f"insufficient credits; need at least {amount:g} tokens")
    set_committed_value(user, "credit_balance", balance)


async def credit_tokens(db: AsyncSession, user: User, amount: float) -> None:
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Token credit must be positive and finite")
    result = await db.execute(update(User).where(User.id == user.id).values(
        credit_balance=func.coalesce(User.credit_balance, 0) + amount,
    ).returning(User.credit_balance))
    set_committed_value(user, "credit_balance", result.scalar_one())
