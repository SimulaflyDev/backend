from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)
    referred_by_code: str | None = Field(default=None, max_length=40)
    terms_accepted: bool = False
    privacy_policy_accepted: bool = False
    merchant_agreement_accepted: bool = False
    model_improvement_consent: bool = True


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class GoogleLoginRequest(BaseModel):
    id_token: str = Field(min_length=10)
    referred_by_code: str | None = Field(default=None, max_length=40)
    # Applied only when creating a NEW account, never to an existing choice.
    model_improvement_consent: bool = True


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
