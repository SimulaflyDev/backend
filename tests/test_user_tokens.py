import pytest
from fastapi import HTTPException
from sqlalchemy import update

from app.models.user import User
from app.services.user_tokens import debit_tokens, credit_tokens


@pytest.mark.asyncio
async def test_spending_does_not_overwrite_reward_with_stale_user(db_session, test_user):
    # Simulate a reward written after authentication loaded the user object.
    await db_session.execute(update(User).where(User.id == test_user.id).values(credit_balance=30).execution_options(synchronize_session=False))
    assert test_user.credit_balance == 20
    await debit_tokens(db_session, test_user, 2)
    await db_session.commit()
    await db_session.refresh(test_user)
    assert test_user.credit_balance == 28


@pytest.mark.asyncio
async def test_spending_checks_current_database_balance(db_session, test_user):
    await db_session.execute(update(User).where(User.id == test_user.id).values(credit_balance=1).execution_options(synchronize_session=False))
    assert test_user.credit_balance == 20
    with pytest.raises(HTTPException) as exc:
        await debit_tokens(db_session, test_user, 2)
    assert exc.value.status_code == 402
    await db_session.refresh(test_user)
    assert test_user.credit_balance == 1


@pytest.mark.asyncio
async def test_credits_add_to_current_database_balance(db_session, test_user):
    await db_session.execute(update(User).where(User.id == test_user.id).values(credit_balance=30).execution_options(synchronize_session=False))
    await credit_tokens(db_session, test_user, 20)
    await db_session.commit()
    await db_session.refresh(test_user)
    assert test_user.credit_balance == 50
