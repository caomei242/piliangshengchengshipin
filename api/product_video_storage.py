from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class OssConfig:
    access_key_id: str
    access_key_secret: str
    bucket: str
    endpoint: str
    upload_endpoint: str
    public_base_url: str
    prefix: str
    url_mode: str
    signed_url_expires_seconds: int


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _normalize_endpoint(value: str) -> str:
    value = value.strip()
    value = value.removeprefix("https://").removeprefix("http://")
    return value.rstrip("/")


def _normalize_prefix(value: str) -> str:
    value = value.strip().lstrip("/")
    if value and not value.endswith("/"):
        value += "/"
    return value


def load_oss_config() -> OssConfig | None:
    access_key_id = _env("OSS_ACCESS_KEY_ID")
    access_key_secret = _env("OSS_ACCESS_KEY_SECRET")
    if not access_key_id or not access_key_secret:
        return None

    bucket = _env("OSS_BUCKET", "hlg-team")
    endpoint = _normalize_endpoint(_env("OSS_ENDPOINT", "oss-cn-zhangjiakou.aliyuncs.com"))
    internal_endpoint = _normalize_endpoint(
        _env("OSS_INTERNAL_ENDPOINT", "oss-cn-zhangjiakou-internal.aliyuncs.com")
    )
    use_internal = _env("OSS_USE_INTERNAL_ENDPOINT", "false").lower() in {"1", "true", "yes"}
    upload_endpoint = internal_endpoint if use_internal and internal_endpoint else endpoint
    public_base_url = _env("OSS_PUBLIC_BASE_URL") or f"https://{bucket}.{endpoint}"
    public_base_url = public_base_url.rstrip("/")
    url_mode = _env("OSS_URL_MODE", "signed").lower()
    if url_mode not in {"signed", "public"}:
        url_mode = "signed"
    expires_seconds = int(_env("OSS_SIGNED_URL_EXPIRES_SECONDS", "604800") or "604800")

    return OssConfig(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        bucket=bucket,
        endpoint=endpoint,
        upload_endpoint=upload_endpoint,
        public_base_url=public_base_url,
        prefix=_normalize_prefix(_env("OSS_PREFIX", "ai批量生产视频/")),
        url_mode=url_mode,
        signed_url_expires_seconds=max(60, expires_seconds),
    )


def _sign(secret: str, text: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _quote_key(key: str) -> str:
    return urllib.parse.quote(key, safe="/")


def _object_url(config: OssConfig, key: str) -> str:
    return f"{config.public_base_url}/{_quote_key(key)}"


def _signed_url(config: OssConfig, key: str) -> tuple[str, str | None]:
    if config.url_mode == "public":
        return _object_url(config, key), None

    expires = int(time.time()) + config.signed_url_expires_seconds
    canonical_resource = f"/{config.bucket}/{key}"
    string_to_sign = f"GET\n\n\n{expires}\n{canonical_resource}"
    signature = _sign(config.access_key_secret, string_to_sign)
    query = urllib.parse.urlencode(
        {
            "OSSAccessKeyId": config.access_key_id,
            "Expires": str(expires),
            "Signature": signature,
        }
    )
    expires_at = datetime.fromtimestamp(expires, tz=timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )
    return f"{_object_url(config, key)}?{query}", expires_at


def _put_file(config: OssConfig, path: Path, key: str, content_type: str) -> dict[str, Any]:
    data = path.read_bytes()
    date_header = formatdate(usegmt=True)
    canonical_resource = f"/{config.bucket}/{key}"
    string_to_sign = f"PUT\n\n{content_type}\n{date_header}\n{canonical_resource}"
    signature = _sign(config.access_key_secret, string_to_sign)
    upload_url = f"https://{config.bucket}.{config.upload_endpoint}/{_quote_key(key)}"
    request = urllib.request.Request(
        upload_url,
        data=data,
        headers={
            "Authorization": f"OSS {config.access_key_id}:{signature}",
            "Content-Type": content_type,
            "Date": date_header,
            "Content-Length": str(len(data)),
        },
        method="PUT",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OSS 上传失败 HTTP {error.code}: {detail[:600]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"OSS 上传失败: {error}") from error

    url, expires_at = _signed_url(config, key)
    return {
        "key": key,
        "url": url,
        "expires_at": expires_at,
        "content_type": content_type,
        "size_bytes": len(data),
    }


def _object_key(config: OssConfig, kind: str, item_id: str, suffix: str) -> str:
    now = datetime.now().astimezone()
    return f"{config.prefix}{kind}/{now:%Y}/{now:%m}/{item_id}{suffix}"


def upload_product_video_outputs(
    *,
    item_id: str,
    video_path: Path,
    script_path: Path,
) -> dict[str, Any]:
    config = load_oss_config()
    if config is None:
        return {
            "enabled": False,
            "provider": "aliyun-oss",
            "reason": "oss_not_configured",
        }

    video_key = _object_key(config, "videos", item_id, ".mp4")
    script_key = _object_key(config, "scripts", item_id, ".json")
    video = _put_file(config, video_path, video_key, "video/mp4")
    script = _put_file(config, script_path, script_key, "application/json; charset=utf-8")
    return {
        "enabled": True,
        "provider": "aliyun-oss",
        "bucket": config.bucket,
        "endpoint": config.endpoint,
        "prefix": config.prefix,
        "url_mode": config.url_mode,
        "video": video,
        "script": script,
    }
