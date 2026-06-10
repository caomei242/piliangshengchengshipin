from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any


PIM_ENV_URLS = {
    "dev": "https://gdpim-dev.huanleguang.com",
    "stage": "https://gdpim-stage.huanleguang.com",
    "prod": "https://gdpim.huanleguang.com",
}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return float(value)


def log(message: str, **fields: Any) -> None:
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"[product-video-pim-worker] {message}{(' ' + suffix) if suffix else ''}", flush=True)


class HttpJsonError(RuntimeError):
    def __init__(self, method: str, url: str, status_code: int, body: str) -> None:
        super().__init__(f"{method} {url} failed HTTP {status_code}: {body[:500]}")
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body

    def json_body(self) -> dict[str, Any] | None:
        try:
            parsed = json.loads(self.body)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None


@dataclass
class WorkerConfig:
    pim_base_url: str
    pim_env: str
    pim_task_type: int
    product_video_api_base_url: str
    internal_api_token: str
    empty_queue_sleep_seconds: float
    poll_interval_seconds: float
    request_timeout_seconds: float
    job_timeout_seconds: float
    worker_concurrency: int
    run_once: bool
    user_agent: str = "pixelle-product-video-pim-worker/1.0"

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        pim_env = os.environ.get("PIM_ENV", "dev").strip() or "dev"
        pim_base_url = os.environ.get("PIM_BASE_URL", "").strip() or PIM_ENV_URLS.get(pim_env, "")
        if not pim_base_url:
            raise RuntimeError("PIM_BASE_URL 未配置，或 PIM_ENV 不是 dev/stage/prod")

        internal_api_token = os.environ.get("INTERNAL_API_TOKEN", "").strip()
        if not internal_api_token:
            raise RuntimeError("INTERNAL_API_TOKEN 未配置")

        return cls(
            pim_base_url=pim_base_url.rstrip("/"),
            pim_env=pim_env,
            pim_task_type=env_int("PIM_TASK_TYPE", 1),
            product_video_api_base_url=os.environ.get(
                "PRODUCT_VIDEO_API_BASE_URL",
                "http://127.0.0.1:8000",
            ).rstrip("/"),
            internal_api_token=internal_api_token,
            empty_queue_sleep_seconds=env_float("PIM_EMPTY_QUEUE_SLEEP_SECONDS", 5.0),
            poll_interval_seconds=env_float("PRODUCT_VIDEO_POLL_INTERVAL_SECONDS", 5.0),
            request_timeout_seconds=env_float("PIM_REQUEST_TIMEOUT_SECONDS", 30.0),
            job_timeout_seconds=env_float("PRODUCT_VIDEO_JOB_TIMEOUT_SECONDS", 1800.0),
            worker_concurrency=max(
                1,
                env_int(
                    "PIM_WORKER_CONCURRENCY",
                    env_int("PRODUCT_VIDEO_MAX_CONCURRENCY", 4),
                ),
            ),
            run_once=os.environ.get("PIM_WORKER_ONCE", "").lower() in {"1", "true", "yes"},
        )


def optional_pim_headers(config: WorkerConfig) -> dict[str, str]:
    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": config.user_agent,
    }
    explicit_authorization = os.environ.get("PIM_AUTHORIZATION", "").strip()
    bearer_token = os.environ.get("PIM_API_TOKEN", "").strip()
    if explicit_authorization:
        headers["Authorization"] = explicit_authorization
    elif bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    return headers


def request_json(
    method: str,
    url: str,
    *,
    timeout: float,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise HttpJsonError(method, url, error.code, body) from error

    if not body.strip():
        return {}
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{method} {url} returned non-object JSON")
    return parsed


def is_empty_queue_response(status_code: int, body: dict[str, Any] | None) -> bool:
    if not body:
        return False
    message = str(body.get("message") or "")
    code = body.get("code")
    return message == "没有需要生成的任务" or code in {500, 10005}


def normalize_pim_get_body(body: dict[str, Any]) -> dict[str, Any] | None:
    if is_empty_queue_response(200, body):
        return None

    if body.get("code") == 10000 and isinstance(body.get("data"), dict):
        task = dict(body["data"])
    elif "id" in body:
        task = dict(body)
    elif isinstance(body.get("data"), dict):
        task = dict(body["data"])
    else:
        raise RuntimeError(f"PIM get 返回格式无法识别: {body}")

    if "id" not in task:
        fallback_id = task.get("_generate_item_id") or task.get("generate_item_id") or task.get("item_id")
        if fallback_id is None:
            raise RuntimeError(f"PIM get 返回缺少 id: {body}")
        task["id"] = fallback_id
        task["_pim_id_fallback"] = True

    return task


def get_pim_task(config: WorkerConfig) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"type": config.pim_task_type})
    url = f"{config.pim_base_url}/api/video-tool/get?{query}"
    try:
        body = request_json(
            "GET",
            url,
            timeout=config.request_timeout_seconds,
            headers=optional_pim_headers(config),
        )
    except HttpJsonError as error:
        parsed = error.json_body()
        if is_empty_queue_response(error.status_code, parsed):
            return None
        raise

    return normalize_pim_get_body(body)


def normalize_target_duration(value: Any) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return [14.0, 16.0]
    first = float(value[0])
    second = float(value[1])
    if first <= 0 or second <= 0 or first > second:
        return [14.0, 16.0]
    return [first, second]


def normalize_config_data(config_data: Any) -> dict[str, Any]:
    if isinstance(config_data, dict):
        return config_data
    if isinstance(config_data, str) and config_data.strip():
        try:
            parsed = json.loads(config_data)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def build_product_video_options(config_data: Any) -> dict[str, Any]:
    options = {
        "aspect_ratio": "1:1",
        "target_duration_seconds": [14.0, 16.0],
        "scene_count": 5,
    }
    config_data = normalize_config_data(config_data)

    scene_count = config_data.get("scene_count")
    if scene_count is not None:
        options["scene_count"] = max(1, min(int(scene_count), 8))

    target_duration = config_data.get("target_duration_seconds")
    if target_duration is not None:
        options["target_duration_seconds"] = normalize_target_duration(target_duration)

    tail_padding_seconds = config_data.get("tail_padding_seconds")
    if tail_padding_seconds is not None:
        options["tail_padding_seconds"] = max(0.0, min(float(tail_padding_seconds), 1.0))

    tts_speed = config_data.get("tts_speed")
    if tts_speed is not None:
        options["tts_speed"] = max(0.75, min(float(tts_speed), 1.35))

    return options


def build_product_payload(task: dict[str, Any]) -> dict[str, Any]:
    product = task.get("product")
    if not isinstance(product, dict):
        raise RuntimeError(f"PIM task 缺少 product 对象: id={task.get('id')}")

    image_urls = product.get("image_urls") or []
    if not isinstance(image_urls, list):
        raise RuntimeError(f"PIM task product.image_urls 不是数组: id={task.get('id')}")

    source_images = [
        {
            "url": str(url),
            "image_id": f"pim_{task['id']}_main_{index}",
            "position": f"main_{index}",
            "image_type": "main",
        }
        for index, url in enumerate([url for url in image_urls if url][:8], 1)
    ]

    return {
        "external_product_id": str(product.get("external_product_id") or task.get("item_id") or task["id"]),
        "title": str(product.get("title") or "商品短视频任务"),
        "category": str(product.get("category") or "商品"),
        "source_images": source_images,
        "extra": {
            "pim_video_tool_id": task.get("id"),
            "platform_id": task.get("platform_id"),
            "shop_id": task.get("shop_id"),
            "item_id": task.get("item_id"),
            "_generate_item_id": task.get("_generate_item_id"),
            "_group_id": task.get("_group_id"),
            "_plan_id": task.get("_plan_id"),
        },
    }


def create_local_product_video_job(config: WorkerConfig, task: dict[str, Any]) -> dict[str, Any]:
    task_id = task["id"]
    request_id = f"pim-video-tool-{task_id}"
    payload = {
        "request_id": request_id,
        "source": f"pim-video-tool:{config.pim_env}",
        "options": build_product_video_options(task.get("config_data")),
        "products": [build_product_payload(task)],
    }
    if not payload["products"][0]["source_images"]:
        raise RuntimeError("PIM product.image_urls 为空，无法生成商品视频")

    return request_json(
        "POST",
        f"{config.product_video_api_base_url}/api/product-videos/jobs",
        timeout=config.request_timeout_seconds,
        payload=payload,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.internal_api_token}",
            "Idempotency-Key": request_id,
            "User-Agent": config.user_agent,
        },
    )


def get_local_job(config: WorkerConfig, job_id: str) -> dict[str, Any]:
    return request_json(
        "GET",
        f"{config.product_video_api_base_url}/api/product-videos/jobs/{job_id}",
        timeout=config.request_timeout_seconds,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.internal_api_token}",
            "User-Agent": config.user_agent,
        },
    )


def wait_local_job(config: WorkerConfig, job_id: str) -> dict[str, Any]:
    started_at = time.monotonic()
    while True:
        job = get_local_job(config, job_id)
        items = job.get("items") or []
        if not items:
            raise RuntimeError(f"本地商品视频任务没有 items: {job_id}")
        item = items[0]
        status = item.get("status")
        log(
            "local job status",
            job_id=job_id,
            item_id=item.get("item_id"),
            status=status,
            progress=item.get("progress"),
            stage=item.get("stage"),
        )
        if status in {"succeeded", "failed", "canceled"}:
            return item
        if time.monotonic() - started_at > config.job_timeout_seconds:
            raise RuntimeError(f"本地商品视频任务超时: {job_id}")
        time.sleep(config.poll_interval_seconds)


def submit_pim_result(config: WorkerConfig, task_id: int, *, status: int, video_url: str = "", error_msg: str = "") -> None:
    payload = {
        "id": task_id,
        "status": status,
        "video_url": video_url,
        "error_msg": error_msg,
    }
    request_json(
        "POST",
        f"{config.pim_base_url}/api/video-tool/submit",
        timeout=config.request_timeout_seconds,
        payload=payload,
        headers=optional_pim_headers(config),
    )


def submit_pim_result_with_retry(
    config: WorkerConfig,
    task_id: int,
    *,
    status: int,
    video_url: str = "",
    error_msg: str = "",
) -> bool:
    for attempt in range(1, 4):
        try:
            submit_pim_result(
                config,
                task_id,
                status=status,
                video_url=video_url,
                error_msg=error_msg,
            )
            return True
        except Exception as exc:
            log("submit result failed", pim_task_id=task_id, status=status, attempt=attempt, error=str(exc)[:300])
            time.sleep(min(attempt * 2, 5))
    return False


def error_message_from_item(item: dict[str, Any]) -> str:
    message = str(item.get("error") or "商品视频生成失败")
    return message[:1000]


def process_task(config: WorkerConfig, task: dict[str, Any]) -> None:
    task_id = int(task["id"])
    log(
        "picked task",
        pim_task_id=task_id,
        id_fallback=bool(task.get("_pim_id_fallback")),
        product_id=(task.get("product") or {}).get("external_product_id"),
    )
    try:
        created = create_local_product_video_job(config, task)
        job_id = created["job_id"]
        item = wait_local_job(config, job_id)
        if item.get("status") == "succeeded" and item.get("video_url"):
            submitted = submit_pim_result_with_retry(
                config,
                task_id,
                status=2,
                video_url=str(item["video_url"]),
                error_msg="",
            )
            log("submitted success" if submitted else "submit success exhausted", pim_task_id=task_id, job_id=job_id)
        else:
            error_msg = error_message_from_item(item)
            submitted = submit_pim_result_with_retry(config, task_id, status=3, error_msg=error_msg)
            log("submitted failure" if submitted else "submit failure exhausted", pim_task_id=task_id, job_id=job_id, error=error_msg)
    except Exception as exc:
        error_msg = f"worker_internal_error: {str(exc)[:900]}"
        log("task failed before normal completion", pim_task_id=task_id, error=error_msg)
        submit_pim_result_with_retry(config, task_id, status=3, error_msg=error_msg)


def run_worker_loop(config: WorkerConfig, *, worker_index: int, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            task = get_pim_task(config)
        except Exception as exc:
            log(
                "get task failed",
                worker=worker_index,
                error=str(exc)[:500],
                sleep_seconds=config.empty_queue_sleep_seconds,
            )
            if config.run_once:
                raise
            stop_event.wait(config.empty_queue_sleep_seconds)
            continue
        if task is None:
            log("queue empty", worker=worker_index, sleep_seconds=config.empty_queue_sleep_seconds)
            if config.run_once:
                break
            stop_event.wait(config.empty_queue_sleep_seconds)
            continue

        process_task(config, task)
        if config.run_once:
            break


def main() -> None:
    config = WorkerConfig.from_env()
    stop_event = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()
        log("received stop signal")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log(
        "started",
        pim_env=config.pim_env,
        pim_base_url=config.pim_base_url,
        task_type=config.pim_task_type,
        product_video_api=config.product_video_api_base_url,
        worker_concurrency=1 if config.run_once else config.worker_concurrency,
    )

    worker_count = 1 if config.run_once else config.worker_concurrency
    if worker_count == 1:
        run_worker_loop(config, worker_index=1, stop_event=stop_event)
    else:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="pim-video-worker") as executor:
            futures = [
                executor.submit(run_worker_loop, config, worker_index=index, stop_event=stop_event)
                for index in range(1, worker_count + 1)
            ]
            while not stop_event.is_set():
                for future in futures:
                    if future.done():
                        future.result()
                        stop_event.set()
                        break
                time.sleep(1)

    log("stopped")


if __name__ == "__main__":
    main()
