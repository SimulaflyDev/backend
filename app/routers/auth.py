import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, delete

from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.models.otp import OTP
from app.schemas.auth import (
    GoogleLoginRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)
from app.schemas.user import UserOut
from app.services.google_auth import GoogleAuthError, verify_id_token
from app.utils.dependencies import DBSession, CurrentUser
from app.utils.email import send_otp_email

router = APIRouter(prefix="/auth", tags=["auth"])

EMAIL_OTP_TTL = timedelta(minutes=10)
EMAIL_OTP_RESEND_COOLDOWN = timedelta(seconds=60)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _send_email_verification_otp(email: str, db: DBSession) -> bool:
    """Send at most one verification email per address during the cooldown window."""
    now = datetime.now(timezone.utc)
    existing = (
        await db.execute(
            select(OTP)
            .where(OTP.target == email)
            .order_by(OTP.created_at.desc())
        )
    ).scalars().first()

    if (
        existing is not None
        and _as_utc(existing.expires_at) > now
        and _as_utc(existing.created_at) >= now - EMAIL_OTP_RESEND_COOLDOWN
    ):
        return False

    await db.execute(delete(OTP).where(OTP.target == email))
    otp_entry = OTP(
        target=email,
        code=f"{secrets.randbelow(900_000) + 100_000}",
        expires_at=now + EMAIL_OTP_TTL,
    )
    db.add(otp_entry)
    await db.commit()

    if not send_otp_email(email, otp_entry.code):
        await db.execute(delete(OTP).where(OTP.id == otp_entry.id))
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to send verification email. Please try again.",
        )

    return True


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: DBSession) -> User:
    existing = await db.execute(select(User).where(User.email == body.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")
    merchant_agreements = None
    if body.terms_accepted or body.privacy_policy_accepted or body.merchant_agreement_accepted:
        if not (
            body.terms_accepted
            and body.privacy_policy_accepted
            and body.merchant_agreement_accepted
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Terms, Privacy Policy, and Merchant Agreement must all be accepted",
            )
        from datetime import datetime, timezone

        merchant_agreements = {
            "accepted_at": datetime.now(timezone.utc).isoformat(),
            "versions": {
                "terms_and_conditions": "2026-08-18",
                "privacy_policy": "2026-08-18",
                "merchant_agreement": "2026-08-18",
            },
        }
    user = User(
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        model_improvement_consent=body.model_improvement_consent,
        is_email_verified=False,
        design_profile={"merchant_registration_agreements": merchant_agreements}
        if merchant_agreements
        else {},
    )
    db.add(user)
    await db.flush()

    # Process referral reward if code is provided
    if body.referred_by_code:
        code_upper = body.referred_by_code.strip().upper()
        from app.models.merchant import Merchant
        from app.models.wallet import Wallet
        from app.models.event import LedgerEntry
        from decimal import Decimal

        res_m = await db.execute(select(Merchant).where(Merchant.referral_code == code_upper))
        referrer_merchant = res_m.scalar_one_or_none()
        if referrer_merchant:
            user.referred_by_code = code_upper
            
            res_w = await db.execute(select(Wallet).where(Wallet.merchant_id == referrer_merchant.id))
            m_wallet = res_w.scalar_one_or_none()
            if not m_wallet:
                m_wallet = Wallet(merchant_id=referrer_merchant.id, balance=Decimal("0.00"))
                db.add(m_wallet)
                await db.flush()
            
            m_wallet.balance += Decimal("50.00")
            
            ledger = LedgerEntry(
                merchant_id=referrer_merchant.id,
                wallet_id=m_wallet.id,
                entry_type="credit",
                amount=Decimal("50.00"),
                reason="user_referral",
                balance_after=m_wallet.balance,
                notes=f"Referral reward for new mobile user {user.email}"
            )
            db.add(ledger)

    await db.commit()
    await db.refresh(user)

    return user


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest, db: DBSession) -> TokenPair:
    res = await db.execute(select(User).where(User.email == body.email.lower()))
    user = res.scalar_one_or_none()
    if (
        not user
        or not user.hashed_password
        or not verify_password(body.password, user.hashed_password)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="account disabled")
    sub = str(user.id)
    return TokenPair(access_token=create_access_token(sub), refresh_token=create_refresh_token(sub))


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest) -> TokenPair:
    try:
        sub = decode_token(body.refresh_token, expected_type="refresh")
    except TokenError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))
    return TokenPair(access_token=create_access_token(sub), refresh_token=create_refresh_token(sub))


@router.post("/google", response_model=TokenPair)
async def google_login(body: GoogleLoginRequest, db: DBSession) -> TokenPair:
    try:
        identity = verify_id_token(body.id_token)
    except GoogleAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    # Resolve user: by google_sub, else by email (link), else create.
    res = await db.execute(select(User).where(User.google_sub == identity.sub))
    user = res.scalar_one_or_none()

    if user is None:
        res = await db.execute(select(User).where(User.email == identity.email))
        user = res.scalar_one_or_none()
        if user is not None:
            user.google_sub = identity.sub
            if not user.full_name and identity.full_name:
                user.full_name = identity.full_name
            if not user.avatar_url and identity.picture:
                user.avatar_url = identity.picture
        else:
            user = User(
                email=identity.email,
                hashed_password=None,
                google_sub=identity.sub,
                model_improvement_consent=body.model_improvement_consent,
                full_name=identity.full_name,
                avatar_url=identity.picture,
                is_active=True,
            )
            db.add(user)
            await db.flush()

            # Process referral reward if code is provided
            if body.referred_by_code:
                code_upper = body.referred_by_code.strip().upper()
                from app.models.merchant import Merchant
                from app.models.wallet import Wallet
                from app.models.event import LedgerEntry
                from decimal import Decimal

                res_m = await db.execute(select(Merchant).where(Merchant.referral_code == code_upper))
                referrer_merchant = res_m.scalar_one_or_none()
                if referrer_merchant:
                    user.referred_by_code = code_upper
                    
                    res_w = await db.execute(select(Wallet).where(Wallet.merchant_id == referrer_merchant.id))
                    m_wallet = res_w.scalar_one_or_none()
                    if not m_wallet:
                        m_wallet = Wallet(merchant_id=referrer_merchant.id, balance=Decimal("0.00"))
                        db.add(m_wallet)
                        await db.flush()
                    
                    m_wallet.balance += Decimal("50.00")
                    
                    ledger = LedgerEntry(
                        merchant_id=referrer_merchant.id,
                        wallet_id=m_wallet.id,
                        entry_type="credit",
                        amount=Decimal("50.00"),
                        reason="user_referral",
                        balance_after=m_wallet.balance,
                        notes=f"Referral reward for new mobile user {user.email}"
                    )
                    db.add(ledger)

    if user.is_active is False:
        user.is_active = True

    await db.commit()
    await db.refresh(user)
    sub = str(user.id)
    return TokenPair(access_token=create_access_token(sub), refresh_token=create_refresh_token(sub))


@router.post("/send-otp")
async def send_otp(user: CurrentUser, db: DBSession):
    sent = await _send_email_verification_otp(user.email, db)
    message = (
        "Verification code sent successfully to your email."
        if sent
        else "A verification code was already sent. Please use the latest email."
    )
    return {"message": message}


class VerifyOtpRequest(BaseModel):
    otp: str


@router.post("/verify-otp")
async def verify_otp(body: VerifyOtpRequest, user: CurrentUser, db: DBSession):
    res = await db.execute(
        select(OTP)
        .where(OTP.target == user.email)
        .order_by(OTP.created_at.desc())
    )
    otp_entry = res.scalars().first()

    if not otp_entry:
        raise HTTPException(status_code=400, detail="No verification code found. Please request a new one.")

    now = datetime.now(timezone.utc)
    if _as_utc(otp_entry.expires_at) < now:
        raise HTTPException(status_code=400, detail="Verification code has expired. Please request a new one.")

    if otp_entry.code != body.otp:
        raise HTTPException(status_code=400, detail="Invalid verification code. Please try again.")

    # Clear verified OTP
    await db.execute(delete(OTP).where(OTP.target == user.email))
    
    user.is_email_verified = True
    await db.commit()
    await db.refresh(user)

    return {"message": "Email verified successfully.", "user": UserOut.model_validate(user)}


class SendMobileOtpRequest(BaseModel):
    phone: str


@router.post("/send-mobile-otp")
async def send_mobile_otp(body: SendMobileOtpRequest, user: CurrentUser, db: DBSession):
    import random
    from datetime import datetime, timedelta, timezone
    from app.models.otp import OTP

    phone = body.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number is required.")

    await db.execute(delete(OTP).where(OTP.target == phone))

    otp_code = f"{random.randint(100000, 999999)}"
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    otp_entry = OTP(target=phone, code=otp_code, expires_at=expires_at)
    db.add(otp_entry)
    await db.commit()

    print(f"\n========================================\n[MOBILE OTP] Sent to {phone}: {otp_code}\n========================================\n")

    return {
        "message": "OTP sent successfully to mobile number.",
        "dev_otp": otp_code
    }


class VerifyMobileOtpRequest(BaseModel):
    phone: str
    otp: str


@router.post("/verify-mobile-otp")
async def verify_mobile_otp(body: VerifyMobileOtpRequest, user: CurrentUser, db: DBSession):
    from datetime import datetime, timezone
    from app.models.otp import OTP

    phone = body.phone.strip()
    res = await db.execute(
        select(OTP)
        .where(OTP.target == phone)
        .order_by(OTP.created_at.desc())
    )
    otp_entry = res.scalars().first()

    if not otp_entry:
        raise HTTPException(status_code=400, detail="No verification code found. Please send OTP again.")

    now = datetime.now(timezone.utc)
    if otp_entry.expires_at < now:
        raise HTTPException(status_code=400, detail="Verification code has expired. Please send OTP again.")

    if otp_entry.code != body.otp:
        raise HTTPException(status_code=400, detail="Invalid verification code. Please try again.")

    await db.execute(delete(OTP).where(OTP.target == phone))
    user.phone = phone
    await db.commit()

    return {"message": "Mobile number verified successfully."}
