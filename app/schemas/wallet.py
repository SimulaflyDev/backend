import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WalletStatusLiteral = Literal["active", "depleted", "frozen"]
TransactionStatusLiteral = Literal["pending", "successful", "failed"]
RateTypeLiteral = Literal["fixed", "percentage"]
PaymentMethodLiteral = Literal["upi", "card", "netbanking", "wallet"]


class WalletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    merchant_id: uuid.UUID
    balance: float
    currency: str
    status: WalletStatusLiteral
    low_balance_threshold: float
    last_recharged_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WalletSettingsUpdate(BaseModel):
    low_balance_threshold: float | None = Field(default=None, ge=0, le=1_000_000)


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    merchant_id: uuid.UUID
    amount: float
    currency: str
    payment_method: str | None
    gateway: str
    gateway_ref: str | None
    razorpay_order_id: str | None
    status: TransactionStatusLiteral
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class PaginatedTransactions(BaseModel):
    items: list[TransactionOut]
    total: int
    limit: int
    offset: int


class TopupIntentRequest(BaseModel):
    amount: int = Field(ge=500, le=500_000)
    currency: Literal["INR"] = "INR"


class TopupIntentResponse(BaseModel):
    order_id: str
    razorpay_key_id: str
    amount: float
    currency: str
    transaction_id: uuid.UUID


class TopupConfirmRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=120)
    payment_id: str = Field(min_length=1, max_length=120)
    signature: str = Field(min_length=1, max_length=256)


class PricingRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    merchant_id: uuid.UUID | None
    rate: float
    rate_type: RateTypeLiteral
    currency: str
    effective_from: datetime
    effective_until: datetime | None
    notes: str | None


class BalanceHistoryItemProduct(BaseModel):
    title: str
    sku: str
    image_url: str | None = None


class BalanceHistoryOrderItem(BaseModel):
    product_id: str | None = None
    title: str
    sku: str
    quantity: int


class BalanceHistoryOrderDetails(BaseModel):
    id: str
    created_at: datetime
    accepted_at: datetime | None = None
    status: str
    buyer_name: str | None = None
    buyer_address: str | None = None
    total_value: float
    items: list[BalanceHistoryOrderItem]


class BalanceHistoryItem(BaseModel):
    id: str
    created_at: datetime
    amount: float
    entry_type: str  # e.g., "Mention", "Add to Cart", "View", "Deposit", "Credit"
    reason: str
    payment_method: str | None = None
    gateway_ref: str | None = None
    gateway: str | None = None
    payment_order_id: str | None = None
    reference_id: str | None = None
    running_balance: float
    product: BalanceHistoryItemProduct | None = None
    order: BalanceHistoryOrderDetails | None = None


class BalanceHistoryResponse(BaseModel):
    items: list[BalanceHistoryItem]
    total: int
    limit: int
    offset: int


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=50)


class RedeemResponse(BaseModel):
    message: str
    balance: float
    credit_amount: float

