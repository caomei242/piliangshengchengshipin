from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from pixelle_video import pixelle_video
from pixelle_video.config import config_manager
from pixelle_video.pipelines.asset_based import AssetBasedPipeline


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_ROOT = Path("/Users/gd/Desktop/个人/AI短视频试验/pixelle-product-test-3814460745354183082")
TEST_ROOT = Path(os.environ.get("PRODUCT_TEST_ROOT", DEFAULT_TEST_ROOT))
ASSET_DIR = TEST_ROOT / "assets"
RESULT_DIR = TEST_ROOT / "result"
PRODUCT_TITLE = os.environ.get("PRODUCT_TITLE", "浅鹅黄假两件Polo衫")
PRODUCT_CATEGORY = os.environ.get("PRODUCT_CATEGORY", "T恤/Polo衫")
ASSET_FILES = [
    name.strip()
    for name in os.environ.get("PRODUCT_ASSET_FILES", "main_1.png,main_2.png,main_3.png,main_4.png,main_5.png").split(",")
    if name.strip()
]
TAIL_PADDING_SECONDS = float(os.environ.get("PRODUCT_TAIL_PADDING", "0.3"))
TTS_SPEED = float(os.environ.get("PRODUCT_TTS_SPEED", "1.08"))


SCRIPT_MODE = os.environ.get("PRODUCT_SCRIPT_MODE", "vision_manual")


ASSET_DESCRIPTIONS = {
    "main_1.png": "室内自然光场景，模特穿浅鹅黄色假两件 Polo/T 恤，白色翻领和白色短袖形成层次，胸前有黑色爱心形装饰，搭配浅蓝牛仔短裤，整体休闲清爽。",
    "main_2.png": "半身商品展示图，突出浅鹅黄色上衣的宽松版型、白色翻领、V 形领口拼接、袖口撞色边和胸前黑色爱心标识。",
    "main_3.png": "户外咖啡休闲场景，模特穿同款浅鹅黄色假两件 Polo/T 恤，搭配牛仔短裤和白色运动鞋，手拿咖啡和帆布包，强调日常通勤和休闲穿搭。",
    "main_4.png": "正面近景展示，模特站姿穿着浅鹅黄色假两件上衣，能看到衣身宽松、不贴身，白色袖子和领口干净利落。",
    "main_5.png": "细节近景图，画面文字写着“假两件设计 / 层次感穿搭”，突出领口、肩部拼接、袖口条纹和胸前黑色图案。",
}


def prepend_codex_bin_if_usable() -> None:
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
    os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"


async def fake_image_analysis(path: str, source: str = "selfhost", **_: object) -> str:
    if SCRIPT_MODE == "no_vision":
        return f"{Path(path).name}，用户提供的商品主图；未做图片内容识别。"
    return ASSET_DESCRIPTIONS.get(Path(path).name, "商品主图，展示浅色 Polo/T 恤的外观和搭配效果。")


class ManualAssetPipeline(AssetBasedPipeline):
    async def initialize_storyboard(self, context):
        context = await super().initialize_storyboard(context)
        context.config.frame_template = "1080x1080/asset_product_square.html"
        context.config.media_width = 1024
        context.config.media_height = 1024
        return context

    async def generate_content(self, context):
        assets = context.request["assets"]
        if SCRIPT_MODE == "ark_multimodal":
            script_path = RESULT_DIR / "ark-multimodal-script.json"
            script_data = json.loads(script_path.read_text(encoding="utf-8"))
            asset_by_name = {Path(path).name: path for path in assets}
            fallback_assets = list(asset_by_name.values())
            context.script = [
                {
                    "scene_number": index,
                    "asset_path": asset_by_name.get(scene.get("image"), fallback_assets[index - 1]),
                    "narrations": [scene["voice"]],
                    "duration": scene.get("duration", 4),
                }
                for index, scene in enumerate(script_data["scenes"], 1)
            ]
        elif SCRIPT_MODE == "no_vision":
            context.script = [
                {
                    "scene_number": 1,
                    "asset_path": assets[0],
                    "narrations": ["这是一件浅鹅黄假两件 Polo 衫，整体走清爽日常路线。"],
                    "duration": 4,
                },
                {
                    "scene_number": 2,
                    "asset_path": assets[1],
                    "narrations": ["假两件的好处，是不用额外想内搭，出门更省心。"],
                    "duration": 4,
                },
                {
                    "scene_number": 3,
                    "asset_path": assets[2],
                    "narrations": ["Polo 衫本身比较好搭，通勤和休闲场景都能用。"],
                    "duration": 4,
                },
                {
                    "scene_number": 4,
                    "asset_path": assets[3],
                    "narrations": ["浅鹅黄色比基础白色更有辨识度，但又不会太跳。"],
                    "duration": 4,
                },
                {
                    "scene_number": 5,
                    "asset_path": assets[4],
                    "narrations": ["想要一件轻松、干净、好搭的上衣，可以重点看这一款。"],
                    "duration": 5,
                },
            ]
        else:
            context.script = [
                {
                    "scene_number": 1,
                    "asset_path": assets[0],
                    "narrations": ["第一眼是清爽的浅鹅黄色，白色翻领做出干净的层次感。"],
                    "duration": 4,
                },
                {
                    "scene_number": 2,
                    "asset_path": assets[1],
                    "narrations": ["宽松版型不贴身，日常单穿也有轻松的轮廓。"],
                    "duration": 4,
                },
                {
                    "scene_number": 3,
                    "asset_path": assets[2],
                    "narrations": ["搭配牛仔短裤和小白鞋，通勤、出游、咖啡店都很自然。"],
                    "duration": 4,
                },
                {
                    "scene_number": 4,
                    "asset_path": assets[3],
                    "narrations": ["袖口和 V 领的撞色边，让基础款多一点学院感。"],
                    "duration": 4,
                },
                {
                    "scene_number": 5,
                    "asset_path": assets[4],
                    "narrations": ["假两件设计不用费心搭配，穿上就是柔和又有层次的一套。"],
                    "duration": 5,
                },
            ]
        return context


async def main() -> None:
    api_key = os.environ.get("GRSAI_API_KEY")

    prepend_codex_bin_if_usable()

    if api_key:
        config_manager.set_llm_config(
            api_key=api_key,
            base_url="https://grsaiapi.com/v1",
            model="gemini-3-pro",
        )
    config_manager.update(
        {
            "comfyui": {
                "tts": {
                    "inference_mode": "local",
                    "local": {
                        "voice": "zh-CN-XiaoxiaoNeural",
                        "speed": 1.08,
                    },
                }
            }
        }
    )

    assets = [str(ASSET_DIR / name) for name in ASSET_FILES]
    missing = [path for path in assets if not Path(path).exists()]
    if missing:
        raise FileNotFoundError(f"Missing assets: {missing}")

    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    await pixelle_video.initialize()
    pixelle_video.image_analysis = fake_image_analysis

    pipeline = ManualAssetPipeline(pixelle_video)
    context = await pipeline(
        assets=assets,
        video_title=PRODUCT_TITLE,
        intent=(
            "基于 5 张商品主图生成一条 14 到 16 秒 1:1 商品展示短视频。"
            f"商品类目：{PRODUCT_CATEGORY}。"
            "风格干净、真实、克制，突出商品外观、日常搭配和可见细节。"
            "不要虚构品牌、价格、材质、活动或功能承诺。每个场景只写一句短旁白。"
        ),
        duration=20,
        source="selfhost",
        voice_id="zh-CN-XiaoxiaoNeural",
        tts_speed=TTS_SPEED,
        narration_tail_padding=TAIL_PADDING_SECONDS,
        bgm_path=None,
        bgm_volume=0,
    )

    final_path = Path(context.final_video_path)
    suffix = os.environ.get("PRODUCT_OUTPUT_SUFFIX")
    if not suffix:
        if SCRIPT_MODE == "ark_multimodal":
            suffix = "square-ark-multimodal"
        elif SCRIPT_MODE == "no_vision":
            suffix = "square-no-vision"
        else:
            suffix = "square"
    copied_video = RESULT_DIR / f"pixelle-product-test-{suffix}.mp4"
    shutil.copy2(final_path, copied_video)

    script_path = RESULT_DIR / f"script-{suffix}.json"
    with script_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "title": context.title,
                "final_video_path": str(final_path),
                "copied_video": str(copied_video),
                "script": getattr(context, "script", None),
                "storyboard_frames": [
                    {
                        "index": frame.index,
                        "narration": frame.narration,
                        "image_path": frame.image_path,
                        "duration": frame.duration,
                        "video_segment_path": frame.video_segment_path,
                    }
                    for frame in context.storyboard.frames
                ],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(json.dumps({"video": str(copied_video), "script": str(script_path)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
