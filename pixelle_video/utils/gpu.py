from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path

from loguru import logger


def _env_flag(name: str, default: str = "auto") -> str:
    return os.environ.get(name, default).strip().lower()


@lru_cache(maxsize=1)
def has_cuda_gpu() -> bool:
    mode = _env_flag("PIXELLE_USE_CUDA", "auto")
    if mode in {"0", "false", "no", "off", "cpu", "disable", "disabled"}:
        return False
    if mode in {"1", "true", "yes", "on", "cuda", "gpu", "force"}:
        return True

    cuda_visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
    if cuda_visible_devices is not None and cuda_visible_devices.strip() in {"", "-1", "none", "void"}:
        return False

    nvidia_visible_devices = os.environ.get("NVIDIA_VISIBLE_DEVICES")
    if nvidia_visible_devices is not None and nvidia_visible_devices.strip().lower() in {"", "none", "void"}:
        return False

    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0 and "GPU" in result.stdout:
            return True
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    return any(Path("/dev").glob("nvidia[0-9]*"))


@lru_cache(maxsize=4)
def ffmpeg_has_encoder(encoder: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and encoder in result.stdout


@lru_cache(maxsize=1)
def should_use_nvenc() -> bool:
    mode = _env_flag("PIXELLE_FFMPEG_ENCODER", "auto")
    if mode in {"libx264", "x264", "cpu"}:
        return False
    if mode in {"h264_nvenc", "nvenc"}:
        return True
    return has_cuda_gpu() and ffmpeg_has_encoder("h264_nvenc")


def ffmpeg_video_encoder_kwargs(
    *,
    cpu_preset: str = "medium",
    crf: int = 23,
    bitrate: str | None = None,
) -> dict[str, object]:
    if should_use_nvenc():
        kwargs: dict[str, object] = {
            "vcodec": "h264_nvenc",
            "pix_fmt": "yuv420p",
            "preset": os.environ.get("PIXELLE_NVENC_PRESET", "medium"),
            "cq": int(os.environ.get("PIXELLE_NVENC_CQ", str(crf))),
        }
    else:
        kwargs = {
            "vcodec": "libx264",
            "pix_fmt": "yuv420p",
            "preset": cpu_preset,
            "crf": crf,
        }
    if bitrate:
        kwargs["b:v"] = bitrate
    return kwargs


def chromium_gpu_args(*, force_cpu: bool = False) -> list[str]:
    mode = _env_flag("PIXELLE_CHROMIUM_GPU", "off")
    if force_cpu:
        return ["--disable-gpu"]
    if mode in {"0", "false", "no", "off", "cpu", "disable", "disabled"}:
        return ["--disable-gpu"]
    if mode in {"1", "true", "yes", "on", "cuda", "gpu", "force"} and has_cuda_gpu():
        return [
            "--enable-gpu",
            "--enable-accelerated-video-decode",
            "--ignore-gpu-blocklist",
        ]
    return ["--disable-gpu"]


def log_gpu_status(component: str) -> None:
    logger.info(
        "{} GPU status: cuda_available={}, ffmpeg_encoder={}",
        component,
        has_cuda_gpu(),
        "h264_nvenc" if should_use_nvenc() else "libx264",
    )
