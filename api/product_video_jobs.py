from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from PIL import Image
from loguru import logger

from api.product_video_storage import upload_product_video_outputs
from api.schemas.product_videos import (
    ProductVideoCreateJobRequest,
    ProductVideoCreateJobResponse,
    ProductVideoItemResponse,
    ProductVideoJobResponse,
    ProductVideoJobSummary,
    ProductVideoOptions,
    ProductVideoProduct,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
ARK_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
MAIN_IMAGE_TYPE_MARKERS = {
    "main",
    "mainimage",
    "main_image",
    "primary",
    "hero",
    "cover",
    "front",
    "主图",
    "商品主图",
    "首图",
    "头图",
}
NON_MAIN_IMAGE_MARKERS = {
    "sku",
    "spec",
    "specification",
    "parameter",
    "param",
    "detail",
    "description",
    "desc",
    "color",
    "variant",
    "style",
    "size",
    "review",
    "comment",
    "buyer",
    "规格",
    "参数",
    "详情",
    "颜色",
    "款式",
    "尺码",
    "评价",
    "买家",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text.strip())
    cleaned = cleaned.strip("-") or "product"
    return cleaned[:42]


def parse_json_text(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def image_to_data_url(path: Path) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((900, 900))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=86, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def download_image(url: str, target: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()

    with Image.open(BytesIO(data)) as image:
        image.convert("RGB").save(target, format="PNG")


class ProductImagePreparationError(RuntimeError):
    def __init__(self, message: str, run_meta: dict[str, Any]) -> None:
        super().__init__(message)
        self.run_meta = run_meta


def normalize_image_marker(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"[\s_\-./]+", "", value.strip().lower())


def is_main_source_image(image_type: str | None, position: str | None) -> tuple[bool, str | None]:
    normalized_type = normalize_image_marker(image_type)
    normalized_position = normalize_image_marker(position)
    markers = [marker for marker in (normalized_type, normalized_position) if marker]

    if any(non_main in marker for marker in markers for non_main in NON_MAIN_IMAGE_MARKERS):
        return False, "non_main_image"

    if any(marker in MAIN_IMAGE_TYPE_MARKERS for marker in markers):
        return True, None

    if any(marker.startswith("main") or marker.startswith("主图") for marker in markers):
        return True, None

    if not normalized_type and normalized_position.isdigit():
        return True, None

    return False, "missing_main_marker"


def source_image_candidates(product: ProductVideoProduct) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    skipped_images: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for index, source_image in enumerate(product.source_images, 1):
        url = str(source_image.url)
        seen_urls.add(url)
        is_main_image, skip_reason = is_main_source_image(
            source_image.image_type,
            source_image.position,
        )
        if not is_main_image:
            skipped_images.append(
                {
                    "url": url,
                    "image_id": source_image.image_id or f"source_{index}",
                    "position": source_image.position,
                    "image_type": source_image.image_type,
                    "source": "source_images",
                    "source_index": index,
                    "skip_reason": skip_reason,
                }
            )
            continue

        candidates.append(
            {
                "url": url,
                "image_id": source_image.image_id or f"source_{index}",
                "position": source_image.position or str(index),
                "image_type": source_image.image_type or "source",
                "width": source_image.width,
                "height": source_image.height,
                "source_state": source_image.source_state,
                "quality_flags": source_image.quality_flags,
                "source": "source_images",
                "source_index": index,
            }
        )

    for index, image_url in enumerate(product.image_urls, 1):
        url = str(image_url)
        if url in seen_urls:
            continue
        candidates.append(
            {
                "url": url,
                "image_id": f"image_{index}",
                "position": str(index),
                "image_type": "legacy_url",
                "width": None,
                "height": None,
                "source_state": None,
                "quality_flags": [],
                "source": "image_urls",
                "source_index": index,
            }
        )

    return candidates[:8], skipped_images


def material_level(original_count: int, target_count: int) -> str:
    if original_count <= 0:
        return "unusable"
    if original_count >= target_count:
        return "complete"
    return "insufficient"


def prepare_product_run(
    runs_root: Path,
    job_id: str,
    item_id: str,
    product: ProductVideoProduct,
    options: ProductVideoOptions,
) -> tuple[Path, list[Path], dict[str, Any]]:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = runs_root / f"{timestamp}-{job_id}-{item_id}-{slugify(product.title)}"
    asset_dir = run_dir / "assets"
    result_dir = run_dir / "result"
    asset_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    target_count = max(1, min(options.scene_count, 8))
    candidates, skipped_images = source_image_candidates(product)
    submitted_image_count = len(product.source_images) + len(product.image_urls)
    if not candidates:
        raise ProductImagePreparationError(
            "source_images_unusable: no usable main image URLs submitted",
            {
                "target_scene_count": target_count,
                "downloaded_image_count": 0,
                "submitted_image_count": submitted_image_count,
                "main_image_candidate_count": 0,
                "skipped_image_count": len(skipped_images),
                "skipped_images": skipped_images,
                "material_level": "unusable",
                "selected_images": [],
                "download_errors": [],
            },
        )

    image_paths: list[Path] = []
    selected_images: list[dict[str, Any]] = []
    download_errors: list[dict[str, Any]] = []

    for candidate in candidates:
        if len(image_paths) >= target_count:
            break
        sequence = len(image_paths) + 1
        target = asset_dir / f"main_{sequence}.png"
        try:
            download_image(candidate["url"], target)
        except Exception as exc:
            download_errors.append(
                {
                    "image_id": candidate["image_id"],
                    "position": candidate["position"],
                    "source": candidate["source"],
                    "error": str(exc)[-300:],
                }
            )
            continue

        image_paths.append(target)
        selected_images.append(
            {
                "sequence": sequence,
                "file": target.name,
                "url": candidate["url"],
                "image_id": candidate["image_id"],
                "position": candidate["position"],
                "image_type": candidate["image_type"],
                "width": candidate["width"],
                "height": candidate["height"],
                "source_state": candidate["source_state"],
                "quality_flags": candidate["quality_flags"],
                "source": candidate["source"],
                "source_index": candidate["source_index"],
                "reused": False,
            }
        )

    original_count = len(image_paths)
    run_meta = {
        "target_scene_count": target_count,
        "downloaded_image_count": original_count,
        "submitted_image_count": submitted_image_count,
        "main_image_candidate_count": len(candidates),
        "skipped_image_count": len(skipped_images),
        "skipped_images": skipped_images,
        "material_level": material_level(original_count, target_count),
        "selected_images": selected_images,
        "download_errors": download_errors,
    }
    if original_count <= 0:
        sample_error = download_errors[0]["error"] if download_errors else "no usable image"
        raise ProductImagePreparationError(f"source_images_unusable: {sample_error}", run_meta)
    if original_count < target_count:
        raise ProductImagePreparationError(
            "source_images_insufficient: "
            f"need {target_count} usable main images, got {original_count}, "
            f"submitted {submitted_image_count}, skipped_non_main {len(skipped_images)}",
            run_meta,
        )

    return run_dir, image_paths, run_meta


def call_ark(
    *,
    api_key: str,
    model: str,
    title: str,
    category: str,
    image_paths: list[Path],
    options: ProductVideoOptions,
) -> dict[str, Any]:
    scene_count = max(1, min(options.scene_count, len(image_paths)))
    min_duration, max_duration = options.target_duration_seconds
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "请观察后面商品主图，为电商 1:1 商品短视频生成完整分镜脚本。\n"
                f"商品标题：{title}。类目：{category}。\n"
                f"素材文件依次是 {', '.join(path.name for path in image_paths)}。\n"
                "严格要求：\n"
                "1. 只输出 JSON，不要 Markdown，不要解释。\n"
                '2. JSON 格式：{"visual_summary":"...","scenes":[{"image":"main_1.png","subtitle":"...","voice":"...","duration":2.8}, ...]}，duration 只填预计镜头秒数。\n'
                f"3. 必须输出 {scene_count} 个 scenes，image 必须对应输入文件名，每个素材文件最多用一次。\n"
                f"4. 成片目标总时长 {min_duration:g} 到 {max_duration:g} 秒，由短旁白自然撑起，不要依赖停顿凑时长。\n"
                "5. voice 每段 7 到 12 个汉字，优先 8 到 11 个汉字，适合女声快速商品展示。\n"
                "6. 每段必须是一句能自然说完的完整短句，不要写半句，不要写短碎词。\n"
                "7. 不要写多个逗号串起来的长句。\n"
                "8. 只能说图里可见信息：颜色、形状、图案、场景、结构、玩法。\n"
                "9. 禁止说遮肉显瘦、材质、做工、品牌、价格、活动、功效、质量承诺。\n"
                "10. 不要夸张喊单，不要“必入”“闭眼冲”。"
            ),
        }
    ]

    for index, image_path in enumerate(image_paths, 1):
        content.append({"type": "text", "text": f"图片 {index}: {image_path.name}"})
        content.append({"type": "image_url", "image_url": {"url": image_to_data_url(image_path)}})

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是电商商品图识别和短视频脚本生成器。只输出严格 JSON，不要解释。",
            },
            {"role": "user", "content": content},
        ],
        "temperature": 0.25,
        "max_tokens": 1600,
    }

    request = urllib.request.Request(
        ARK_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8")
        raise RuntimeError(f"火山方舟请求失败 HTTP {error.code}: {detail}") from error

    message = body["choices"][0]["message"]
    script = parse_json_text(message["content"])
    scenes = script.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        raise RuntimeError("火山方舟返回的 scenes 为空")
    valid_images = {path.name for path in image_paths}
    scenes = [scene for scene in scenes if scene.get("image") in valid_images][:scene_count]
    if not scenes:
        raise RuntimeError("火山方舟返回的 scenes 没有匹配输入图片")
    return {
        "model": body.get("model"),
        "usage": body.get("usage"),
        "visual_summary": script.get("visual_summary", ""),
        "scenes": scenes,
    }


def save_script(run_dir: Path, script_payload: dict[str, Any]) -> Path:
    result_dir = run_dir / "result"
    result_dir.mkdir(parents=True, exist_ok=True)
    active_script = result_dir / "ark-multimodal-script.json"
    active_script.write_text(
        json.dumps(script_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return active_script


def get_video_duration(video_path: Path, env: dict[str, str]) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=True,
    )
    return float(result.stdout.strip())


def prepend_codex_bin_if_usable(env: dict[str, str]) -> None:
    bin_dir = REPO_ROOT / ".codex-bin"
    ffmpeg = bin_dir / "ffmpeg"
    ffprobe = bin_dir / "ffprobe"
    if not (ffmpeg.exists() and ffprobe.exists()):
        return
    try:
        for binary in (ffmpeg, ffprobe):
            result = subprocess.run(
                [str(binary), "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                return
    except (OSError, subprocess.SubprocessError):
        return
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"


def compose_video(
    run_dir: Path,
    title: str,
    category: str,
    image_paths: list[Path],
    options: ProductVideoOptions,
) -> tuple[Path, str, dict[str, Any]]:
    env = os.environ.copy()
    prepend_codex_bin_if_usable(env)
    env["PRODUCT_SCRIPT_MODE"] = "ark_multimodal"
    env["PRODUCT_TEST_ROOT"] = str(run_dir)
    env["PRODUCT_TITLE"] = slugify(title)
    env["PRODUCT_CATEGORY"] = category
    env["PRODUCT_ASSET_FILES"] = ",".join(path.name for path in image_paths)
    env["PRODUCT_TAIL_PADDING"] = f"{options.tail_padding_seconds:.2f}"

    def run_once(speed: float, suffix: str) -> tuple[Path, str, float]:
        run_env = env.copy()
        run_env["PRODUCT_OUTPUT_SUFFIX"] = suffix
        run_env["PRODUCT_TTS_SPEED"] = f"{speed:.2f}"
        process = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "run_product_asset_test.py")],
            cwd=REPO_ROOT,
            env=run_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=240,
            check=False,
        )

        video_path = run_dir / "result" / f"pixelle-product-test-{suffix}.mp4"
        if process.returncode != 0 or not video_path.exists():
            raise RuntimeError(process.stdout[-4000:])
        return video_path, process.stdout[-4000:], get_video_duration(video_path, run_env)

    attempts: list[dict[str, Any]] = []
    video_path, log_tail, duration = run_once(options.tts_speed, "square-ui")
    actual_speed = options.tts_speed
    attempts.append({"speed": round(options.tts_speed, 2), "duration": round(duration, 2)})

    min_duration, max_duration = options.target_duration_seconds
    if duration < min_duration or duration > max_duration:
        target_duration = (min_duration + max_duration) / 2
        adjusted_speed = max(0.82, min(1.20, options.tts_speed * duration / target_duration))
        if abs(adjusted_speed - options.tts_speed) >= 0.02:
            video_path, log_tail, duration = run_once(adjusted_speed, "square-ui")
            actual_speed = adjusted_speed
            attempts.append({"speed": round(adjusted_speed, 2), "duration": round(duration, 2)})

    return video_path, log_tail, {
        "duration": duration,
        "tts_speed": actual_speed,
        "attempts": attempts,
    }


class ProductVideoJobManager:
    def __init__(self) -> None:
        default_data_root = REPO_ROOT / "data"
        self.runs_root = Path(
            os.environ.get("PIXELLE_RUNS_ROOT", str(default_data_root / "pixelle-ui-runs"))
        )
        self.state_root = Path(
            os.environ.get("PRODUCT_VIDEO_STATE_ROOT", str(default_data_root / "product-video-jobs"))
        )
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._item_to_job: dict[str, str] = {}
        self._futures: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(int(os.environ.get("PRODUCT_VIDEO_MAX_CONCURRENCY", "4")))
        self._load_jobs()

    def _load_jobs(self) -> None:
        for path in self.state_root.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"Skip invalid product video job state {path}: {exc}")
                continue
            for item in job.get("items", []):
                if item.get("status") in {"queued", "running"}:
                    item["status"] = "failed"
                    item["error"] = "服务重启前任务未完成，请重新提交。"
                    item["progress"] = 100
                self._item_to_job[item["item_id"]] = job["job_id"]
            self._recompute_job_status(job)
            self._jobs[job["job_id"]] = job

    def _state_path(self, job_id: str) -> Path:
        return self.state_root / f"{job_id}.json"

    def _save_job(self, job: dict[str, Any]) -> None:
        job["updated_at"] = now_iso()
        self._recompute_job_status(job)
        self._state_path(job["job_id"]).write_text(
            json.dumps(job, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _recompute_job_status(self, job: dict[str, Any]) -> None:
        statuses = [item.get("status", "queued") for item in job.get("items", [])]
        if not statuses:
            job["status"] = "failed"
        elif all(status == "queued" for status in statuses):
            job["status"] = "queued"
        elif any(status in {"queued", "running"} for status in statuses):
            job["status"] = "running"
        elif all(status == "succeeded" for status in statuses):
            job["status"] = "succeeded"
        else:
            job["status"] = "failed"

    def _summary(self, job: dict[str, Any]) -> ProductVideoJobSummary:
        counts = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0, "canceled": 0}
        for item in job.get("items", []):
            status = item.get("status", "queued")
            if status in counts:
                counts[status] += 1
        return ProductVideoJobSummary(total=len(job.get("items", [])), **counts)

    def _base_url(self, base_url: str) -> str:
        return base_url.rstrip("/")

    def _item_response(self, item: dict[str, Any], base_url: str) -> ProductVideoItemResponse:
        base = self._base_url(base_url)
        item_id = item["item_id"]
        download_url = None
        script_download_url = None
        if item.get("video_path"):
            download_url = f"{base}/api/product-videos/items/{item_id}/video"
        if item.get("script_path"):
            script_download_url = f"{base}/api/product-videos/items/{item_id}/script"

        storage = item.get("storage") if isinstance(item.get("storage"), dict) else None
        storage_video = storage.get("video") if storage else None
        storage_script = storage.get("script") if storage else None
        video_url = storage_video.get("url") if isinstance(storage_video, dict) else None
        script_url = storage_script.get("url") if isinstance(storage_script, dict) else None
        video_url = video_url or download_url
        script_url = script_url or script_download_url
        expires_at = None
        if isinstance(storage_video, dict):
            expires_at = storage_video.get("expires_at")
        if expires_at is None and isinstance(storage_script, dict):
            expires_at = storage_script.get("expires_at")

        return ProductVideoItemResponse(
            item_id=item_id,
            external_product_id=item["external_product_id"],
            title=item["title"],
            status=item["status"],
            progress=item.get("progress", 0),
            stage=item.get("stage"),
            video_url=video_url,
            script_url=script_url,
            download_url=download_url,
            script_download_url=script_download_url,
            oss_key=storage_video.get("key") if isinstance(storage_video, dict) else None,
            script_oss_key=storage_script.get("key") if isinstance(storage_script, dict) else None,
            expires_at=expires_at,
            storage=storage,
            material_level=item.get("material_level"),
            selected_images=item.get("selected_images") or [],
            duration_seconds=item.get("duration_seconds"),
            usage=item.get("usage"),
            error=item.get("error"),
        )

    def _job_response(self, job: dict[str, Any], base_url: str) -> ProductVideoJobResponse:
        base = self._base_url(base_url)
        job_id = job["job_id"]
        return ProductVideoJobResponse(
            job_id=job_id,
            status=job["status"],
            created_at=job["created_at"],
            updated_at=job["updated_at"],
            request_id=job.get("request_id"),
            poll_url=f"{base}/api/product-videos/jobs/{job_id}",
            summary=self._summary(job),
            items=[self._item_response(item, base) for item in job.get("items", [])],
        )

    def create_job(
        self,
        request_body: ProductVideoCreateJobRequest,
        base_url: str,
        idempotency_key: str | None = None,
    ) -> ProductVideoCreateJobResponse:
        dedupe_key = idempotency_key or request_body.request_id
        if dedupe_key:
            for existing_job in self._jobs.values():
                if existing_job.get("idempotency_key") == dedupe_key:
                    response = self._job_response(existing_job, base_url)
                    return ProductVideoCreateJobResponse(
                        job_id=response.job_id,
                        status=response.status,
                        created_at=response.created_at,
                        poll_url=response.poll_url,
                        items=response.items,
                    )

        stamp = time.strftime("%Y%m%d_%H%M%S")
        job_id = f"job_{stamp}_{uuid.uuid4().hex[:8]}"
        created_at = now_iso()
        job: dict[str, Any] = {
            "job_id": job_id,
            "status": "queued",
            "request_id": request_body.request_id,
            "idempotency_key": dedupe_key,
            "source": request_body.source,
            "created_at": created_at,
            "updated_at": created_at,
            "options": request_body.options.model_dump(mode="json"),
            "items": [],
        }

        for index, product in enumerate(request_body.products, 1):
            item_id = f"item_{stamp}_{index:04d}_{uuid.uuid4().hex[:6]}"
            item = {
                "item_id": item_id,
                "external_product_id": product.external_product_id,
                "title": product.title,
                "category": product.category,
                "product": product.model_dump(mode="json"),
                "status": "queued",
                "progress": 0,
                "stage": None,
                "video_path": None,
                "script_path": None,
                "duration_seconds": None,
                "usage": None,
                "storage": None,
                "material_level": None,
                "selected_images": [],
                "error": None,
                "created_at": created_at,
                "updated_at": created_at,
            }
            job["items"].append(item)
            self._item_to_job[item_id] = job_id

        self._jobs[job_id] = job
        self._save_job(job)

        for item in job["items"]:
            future = asyncio.create_task(self._run_item(job_id, item["item_id"]))
            self._futures[item["item_id"]] = future
            future.add_done_callback(lambda _f, item_id=item["item_id"]: self._futures.pop(item_id, None))

        response = self._job_response(job, base_url)
        return ProductVideoCreateJobResponse(
            job_id=response.job_id,
            status=response.status,
            created_at=response.created_at,
            poll_url=response.poll_url,
            items=response.items,
        )

    def get_job(self, job_id: str, base_url: str) -> ProductVideoJobResponse:
        job = self._jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        return self._job_response(job, base_url)

    def get_item(self, job_id: str, item_id: str, base_url: str) -> ProductVideoItemResponse:
        job = self._jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        item = next((entry for entry in job.get("items", []) if entry["item_id"] == item_id), None)
        if not item:
            raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")
        return self._item_response(item, base_url)

    def get_item_paths(self, item_id: str) -> tuple[Path | None, Path | None]:
        job_id = self._item_to_job.get(item_id)
        if not job_id:
            raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")
        job = self._jobs[job_id]
        item = next((entry for entry in job.get("items", []) if entry["item_id"] == item_id), None)
        if not item:
            raise HTTPException(status_code=404, detail=f"Item not found: {item_id}")
        video_path = Path(item["video_path"]) if item.get("video_path") else None
        script_path = Path(item["script_path"]) if item.get("script_path") else None
        return video_path, script_path

    def _update_item(self, job: dict[str, Any], item: dict[str, Any], **updates: Any) -> None:
        item.update(updates)
        item["updated_at"] = now_iso()
        self._save_job(job)

    async def _run_item(self, job_id: str, item_id: str) -> None:
        async with self._semaphore:
            job = self._jobs[job_id]
            item = next(entry for entry in job["items"] if entry["item_id"] == item_id)
            options = ProductVideoOptions(**job["options"])
            product = ProductVideoProduct(**item["product"])

            try:
                api_key = os.environ.get("ARK_API_KEY", "")
                if not api_key:
                    raise RuntimeError("ARK_API_KEY 未配置")

                self._update_item(
                    job,
                    item,
                    status="running",
                    stage="download_images",
                    progress=10,
                    error=None,
                )
                run_dir, image_paths, run_meta = await asyncio.to_thread(
                    prepare_product_run,
                    self.runs_root,
                    job_id,
                    item_id,
                    product,
                    options,
                )
                self._update_item(
                    job,
                    item,
                    material_level=run_meta["material_level"],
                    selected_images=run_meta["selected_images"],
                )

                self._update_item(job, item, stage="generate_script", progress=35)
                script_payload = await asyncio.to_thread(
                    call_ark,
                    api_key=api_key,
                    model=options.model,
                    title=product.title,
                    category=product.category,
                    image_paths=image_paths,
                    options=options,
                )
                script_payload["material_level"] = run_meta["material_level"]
                script_payload["selected_images"] = run_meta["selected_images"]
                script_path = save_script(run_dir, script_payload)
                item["script_path"] = str(script_path)
                item["usage"] = script_payload.get("usage")
                self._update_item(job, item, stage="synthesize_voice", progress=55)

                self._update_item(job, item, stage="compose_video", progress=72)
                video_path, _log, video_meta = await asyncio.to_thread(
                    compose_video,
                    run_dir,
                    product.title,
                    product.category,
                    image_paths,
                    options,
                )

                self._update_item(job, item, stage="upload_oss", progress=88)
                storage = await asyncio.to_thread(
                    upload_product_video_outputs,
                    item_id=item_id,
                    video_path=video_path,
                    script_path=script_path,
                )

                self._update_item(
                    job,
                    item,
                    status="succeeded",
                    stage="save_result",
                    progress=100,
                    video_path=str(video_path),
                    script_path=str(script_path),
                    duration_seconds=round(float(video_meta["duration"]), 2),
                    usage=script_payload.get("usage"),
                    storage=storage,
                    material_level=run_meta["material_level"],
                    selected_images=run_meta["selected_images"],
                    error=None,
                )
            except ProductImagePreparationError as exc:
                logger.warning(f"Product video item image preparation failed: {item_id}: {exc}")
                self._update_item(
                    job,
                    item,
                    status="failed",
                    progress=100,
                    error=str(exc)[-1000:],
                    material_level=exc.run_meta.get("material_level"),
                    selected_images=exc.run_meta.get("selected_images") or [],
                )
            except Exception as exc:
                logger.exception(f"Product video item failed: {item_id}")
                self._update_item(
                    job,
                    item,
                    status="failed",
                    progress=100,
                    error=str(exc)[-1000:],
                )


product_video_job_manager = (
    None if os.environ.get("PRODUCT_VIDEO_SKIP_MANAGER_INIT") == "1" else ProductVideoJobManager()
)
