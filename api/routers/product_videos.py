from __future__ import annotations

import json
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from api.product_video_jobs import product_video_job_manager
from api.schemas.product_videos import (
    ProductVideoCreateJobRequest,
    ProductVideoCreateJobResponse,
    ProductVideoItemResponse,
    ProductVideoJobResponse,
)


router = APIRouter(prefix="/product-videos", tags=["Product Videos"])


def require_internal_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected_token = os.environ.get("INTERNAL_API_TOKEN")
    if not expected_token:
        raise HTTPException(status_code=503, detail="INTERNAL_API_TOKEN 未配置")

    expected_header = f"Bearer {expected_token}"
    if authorization != expected_header:
        raise HTTPException(status_code=401, detail="Invalid Authorization token")


@router.post(
    "/jobs",
    response_model=ProductVideoCreateJobResponse,
    dependencies=[Depends(require_internal_token)],
)
async def create_product_video_job(
    request_body: ProductVideoCreateJobRequest,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ProductVideoCreateJobResponse:
    return product_video_job_manager.create_job(
        request_body=request_body,
        base_url=str(request.base_url),
        idempotency_key=idempotency_key,
    )


@router.get(
    "/jobs/{job_id}",
    response_model=ProductVideoJobResponse,
    dependencies=[Depends(require_internal_token)],
)
async def get_product_video_job(
    job_id: str,
    request: Request,
) -> ProductVideoJobResponse:
    return product_video_job_manager.get_job(job_id, base_url=str(request.base_url))


@router.get(
    "/jobs/{job_id}/items/{item_id}",
    response_model=ProductVideoItemResponse,
    dependencies=[Depends(require_internal_token)],
)
async def get_product_video_item(
    job_id: str,
    item_id: str,
    request: Request,
) -> ProductVideoItemResponse:
    return product_video_job_manager.get_item(
        job_id=job_id,
        item_id=item_id,
        base_url=str(request.base_url),
    )


@router.get(
    "/items/{item_id}/video",
    dependencies=[Depends(require_internal_token)],
)
async def download_product_video(item_id: str) -> FileResponse:
    video_path, _script_path = product_video_job_manager.get_item_paths(item_id)
    if not video_path or not video_path.exists():
        raise HTTPException(status_code=404, detail=f"Video not found for item: {item_id}")
    return FileResponse(
        path=str(video_path),
        media_type="video/mp4",
        filename=f"{item_id}.mp4",
    )


@router.get(
    "/items/{item_id}/script",
    dependencies=[Depends(require_internal_token)],
)
async def download_product_script(item_id: str) -> JSONResponse:
    _video_path, script_path = product_video_job_manager.get_item_paths(item_id)
    if not script_path or not script_path.exists():
        raise HTTPException(status_code=404, detail=f"Script not found for item: {item_id}")
    return JSONResponse(content=json.loads(script_path.read_text(encoding="utf-8")))
