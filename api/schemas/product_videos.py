from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class ProductVideoOptions(BaseModel):
    aspect_ratio: Literal["1:1"] = "1:1"
    target_duration_seconds: tuple[float, float] = (14.0, 16.0)
    scene_count: int = Field(default=5, ge=1, le=8)
    tail_padding_seconds: float = Field(default=0.3, ge=0.0, le=1.0)
    tts_speed: float = Field(default=1.08, ge=0.75, le=1.35)
    model: str = "doubao-seed-2-0-lite-260215"


class ProductVideoSourceImage(BaseModel):
    url: HttpUrl
    image_id: str | None = None
    position: str | None = None
    image_type: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    source_state: str | None = None
    quality_flags: list[str] = Field(default_factory=list)


class ProductVideoProduct(BaseModel):
    external_product_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: str = "商品"
    image_urls: list[HttpUrl] = Field(default_factory=list, max_length=8)
    source_images: list[ProductVideoSourceImage] = Field(default_factory=list, max_length=8)
    extra: dict[str, Any] = Field(default_factory=dict)


class ProductVideoCreateJobRequest(BaseModel):
    request_id: str | None = None
    source: str = "commerce-system"
    options: ProductVideoOptions = Field(default_factory=ProductVideoOptions)
    products: list[ProductVideoProduct] = Field(min_length=1, max_length=50)


class ProductVideoItemResponse(BaseModel):
    item_id: str
    external_product_id: str
    title: str
    status: str
    progress: int = 0
    stage: str | None = None
    video_url: str | None = None
    script_url: str | None = None
    download_url: str | None = None
    script_download_url: str | None = None
    oss_key: str | None = None
    script_oss_key: str | None = None
    expires_at: str | None = None
    storage: dict[str, Any] | None = None
    material_level: str | None = None
    selected_images: list[dict[str, Any]] = Field(default_factory=list)
    duration_seconds: float | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None


class ProductVideoJobSummary(BaseModel):
    total: int
    queued: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    canceled: int = 0


class ProductVideoJobResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    updated_at: str
    request_id: str | None = None
    poll_url: str
    summary: ProductVideoJobSummary
    items: list[ProductVideoItemResponse]


class ProductVideoCreateJobResponse(BaseModel):
    job_id: str
    status: str
    created_at: str
    poll_url: str
    items: list[ProductVideoItemResponse]
