import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CarouselSlide(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    enabled: bool = True
    image_url: str | None = Field(default=None, max_length=2048)
    eyebrow: str = Field(default="", max_length=70)
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=200)
    button_label: str = Field(default="Explore rooms", max_length=36)
    action: Literal["explore_rooms", "scan", "shop", "sessions", "merchant", "product", "none"] = "explore_rooms"
    merchant_lookup: str | None = Field(default=None, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")
    product_id: str | None = Field(default=None, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")

    @field_validator("image_url")
    @classmethod
    def safe_image_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        if re.fullmatch(r"/api/v1/home/carousel/images/[0-9a-fA-F-]{36}", value):
            return value
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password or "\\" in value or any(ord(c) < 32 for c in value):
            raise ValueError("Use an uploaded carousel image or a public HTTPS image URL")
        return value

    @model_validator(mode="after")
    def valid_destination(self):
        if self.action != "none" and not self.button_label:
            raise ValueError("A button label is required for the selected action")
        if self.action in {"merchant", "product"} and not self.merchant_lookup:
            raise ValueError("Select a shop slug or ID for this action")
        if self.action == "product" and not self.product_id:
            raise ValueError("Enter a product ID for this action")
        return self


class CarouselSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    autoplay: bool = True
    interval_seconds: int = Field(default=6, ge=3, le=30)
    slides: list[CarouselSlide] = Field(default_factory=list, max_length=10)

    @field_validator("slides")
    @classmethod
    def unique_slides(cls, slides):
        if len({slide.id for slide in slides}) != len(slides):
            raise ValueError("Slide IDs must be unique")
        return slides


class CarouselUpdate(CarouselSettings):
    version: int = Field(ge=0)


class CarouselOut(CarouselSettings):
    version: int


class CarouselImageUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 5 MB raw stays below the API's 8 MB request-body limit after base64.
    image_base64: str = Field(min_length=1, max_length=7_000_000)


class CarouselImageOut(BaseModel):
    url: str
    byte_size: int


def default_carousel() -> CarouselOut:
    return CarouselOut(version=0, slides=[CarouselSlide(
        id="welcome", eyebrow="INSPIRE · VISUALIZE · SHOP",
        title="Spaces that\nfeel like you.",
        description="Turn your ideas into beautiful spaces with AI.",
    )])
