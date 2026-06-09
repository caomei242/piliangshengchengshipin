from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import streamlit as st
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_SAMPLE_ROOT = Path("/Users/gd/Desktop/个人/AI短视频试验/pixelle-product-test-3814460745354183082")
SAMPLE_ROOT = Path(os.environ["PIXELLE_SAMPLE_ROOT"]) if os.environ.get("PIXELLE_SAMPLE_ROOT") else (
    LOCAL_SAMPLE_ROOT if LOCAL_SAMPLE_ROOT.exists() else Path("/app/data/sample_product")
)
RUNS_ROOT = Path(os.environ.get("PIXELLE_RUNS_ROOT", "/app/data/pixelle-ui-runs"))
ARK_ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DEFAULT_MODEL = "doubao-seed-2-0-lite-260215"
XLSX_NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text.strip())
    cleaned = cleaned.strip("-") or "product"
    return cleaned[:42]


def image_to_data_url(path: Path) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((900, 900))
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=86, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def parse_json_text(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - ord("A") + 1
    return value - 1


def read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []

    values: list[str] = []
    for item in root.findall("x:si", XLSX_NS):
        parts = [node.text or "" for node in item.findall(".//x:t", XLSX_NS)]
        values.append("".join(parts))
    return values


def read_xlsx_rows(xlsx_bytes: bytes) -> list[dict[str, str]]:
    with zipfile.ZipFile(BytesIO(xlsx_bytes)) as archive:
        shared_strings = read_shared_strings(archive)
        root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", XLSX_NS):
        cells: dict[int, str] = {}
        for cell in row.findall("x:c", XLSX_NS):
            ref = cell.attrib.get("r", "")
            value_node = cell.find("x:v", XLSX_NS)
            inline_node = cell.find("x:is/x:t", XLSX_NS)
            value = ""
            if value_node is not None and value_node.text is not None:
                value = value_node.text
                if cell.attrib.get("t") == "s":
                    value = shared_strings[int(value)]
            elif inline_node is not None and inline_node.text is not None:
                value = inline_node.text
            if ref:
                cells[column_index(ref)] = value

        if cells:
            width = max(cells) + 1
            rows.append([cells.get(index, "") for index in range(width)])

    if not rows:
        return []

    headers = [header.strip() for header in rows[0]]
    records: list[dict[str, str]] = []
    for row in rows[1:]:
        record = {
            headers[index]: (row[index].strip() if index < len(row) else "")
            for index in range(len(headers))
            if headers[index]
        }
        if any(record.values()):
            records.append(record)
    return records


def extract_products(records: list[dict[str, str]]) -> list[dict]:
    title_keys = ["商品标题", "宝贝标题", "宝贝旧标题", "标题", "商品名称", "宝贝名称"]
    category_keys = ["商品分类", "类目", "淘宝类目", "分类"]
    id_keys = ["商品编号", "宝贝ID", "商品ID", "ID"]
    products: list[dict] = []

    for index, record in enumerate(records, 1):
        category = next((record.get(key, "") for key in category_keys if record.get(key)), "")
        product_id = next((record.get(key, "") for key in id_keys if record.get(key)), str(index))
        title = next((record.get(key, "") for key in title_keys if record.get(key)), "")
        if not title:
            title = f"{category or '商品'}-{product_id}"

        image_items = [
            (key, value)
            for key, value in record.items()
            if value.startswith("http") and ("图" in key or "image" in key.lower())
        ]
        if not image_items:
            image_items = [
                (key, value)
                for key, value in record.items()
                if value.startswith("http")
            ]

        image_urls = [value for _key, value in sorted(image_items, key=lambda item: item[0])][:6]
        if len(image_urls) < 1:
            continue

        products.append(
            {
                "row": index + 1,
                "product_id": product_id,
                "title": title,
                "category": category or "商品",
                "image_urls": image_urls,
                "source": record,
            }
        )

    return products


def call_ark(
    *,
    api_key: str,
    model: str,
    title: str,
    category: str,
    image_paths: list[Path],
) -> dict:
    content: list[dict] = [
        {
            "type": "text",
            "text": (
                "请观察后面商品主图，为电商 1:1 商品短视频生成完整分镜脚本。\n"
                f"商品标题：{title}。类目：{category}。\n"
                f"素材文件依次是 {', '.join(path.name for path in image_paths)}。\n"
                "严格要求：\n"
                "1. 只输出 JSON，不要 Markdown，不要解释。\n"
                '2. JSON 格式：{"visual_summary":"...","scenes":[{"image":"main_1.png","subtitle":"...","voice":"...","duration":2.8}, ...]}，duration 只填预计镜头秒数。\n'
                "3. 必须输出 5 个 scenes，每张图只用一次，image 必须对应输入文件名。\n"
                "4. 成片目标总时长 14 到 16 秒，由短旁白自然撑起，不要依赖停顿凑时长。\n"
                "5. voice 每段 7 到 12 个汉字，优先 8 到 11 个汉字，适合女声快速商品展示。\n"
                "6. 五段 voice 合计 45 到 58 个汉字。\n"
                "7. 每段必须是一句能自然说完的完整短句，不要写半句，不要写短碎词。\n"
                "8. 不要写多个逗号串起来的长句。\n"
                "9. 只能说图里可见信息：颜色、形状、图案、场景、结构、玩法。\n"
                "10. 禁止说遮肉显瘦、材质、做工、品牌、价格、活动、功效、质量承诺。\n"
                "11. 不要夸张喊单，不要“必入”“闭眼冲”。"
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
    return {
        "model": body.get("model"),
        "usage": body.get("usage"),
        "visual_summary": script.get("visual_summary", ""),
        "scenes": script.get("scenes", []),
    }


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


def prepare_run_dir(title: str, uploads: list, use_sample: bool) -> tuple[Path, list[Path]]:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_ROOT / f"{timestamp}-{slugify(title)}"
    asset_dir = run_dir / "assets"
    result_dir = run_dir / "result"
    asset_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    image_paths: list[Path] = []
    if use_sample:
        for index in range(1, 6):
            source = SAMPLE_ROOT / "assets" / f"main_{index}.png"
            target = asset_dir / f"main_{index}.png"
            shutil.copy2(source, target)
            image_paths.append(target)
    else:
        for index, upload in enumerate(uploads, 1):
            target = asset_dir / f"main_{index}.png"
            with Image.open(upload) as image:
                image.convert("RGB").save(target, format="PNG")
            image_paths.append(target)

    return run_dir, image_paths


def prepare_product_run(product: dict) -> tuple[Path, list[Path]]:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_ROOT / f"{timestamp}-row{product['row']}-{slugify(product['title'])}"
    asset_dir = run_dir / "assets"
    result_dir = run_dir / "result"
    asset_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    image_paths: list[Path] = []
    for index, url in enumerate(product["image_urls"], 1):
        target = asset_dir / f"main_{index}.png"
        download_image(url, target)
        image_paths.append(target)
    return run_dir, image_paths


def save_script(run_dir: Path, script_payload: dict) -> Path:
    result_dir = run_dir / "result"
    result_dir.mkdir(parents=True, exist_ok=True)
    active_script = result_dir / "ark-multimodal-script.json"
    active_script.write_text(json.dumps(script_payload, ensure_ascii=False, indent=2), encoding="utf-8")
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


def compose_video(
    run_dir: Path,
    title: str,
    category: str,
    image_paths: list[Path],
    tail_padding: float,
    tts_speed: float,
) -> tuple[Path, str, dict]:
    env = os.environ.copy()
    bin_dir = REPO_ROOT / ".codex-bin"
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["PRODUCT_SCRIPT_MODE"] = "ark_multimodal"
    env["PRODUCT_TEST_ROOT"] = str(run_dir)
    env["PRODUCT_TITLE"] = slugify(title)
    env["PRODUCT_CATEGORY"] = category
    env["PRODUCT_ASSET_FILES"] = ",".join(path.name for path in image_paths)
    env["PRODUCT_TAIL_PADDING"] = f"{tail_padding:.2f}"

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
            timeout=180,
            check=False,
        )

        video_path = run_dir / "result" / f"pixelle-product-test-{suffix}.mp4"
        if process.returncode != 0 or not video_path.exists():
            raise RuntimeError(process.stdout[-4000:])
        return video_path, process.stdout[-4000:], get_video_duration(video_path, run_env)

    attempts = []
    video_path, log_tail, duration = run_once(tts_speed, "square-ui")
    actual_speed = tts_speed
    attempts.append({"speed": round(tts_speed, 2), "duration": round(duration, 2)})
    if duration < 14.0 or duration > 16.0:
        target_duration = 15.0
        adjusted_speed = max(0.82, min(1.20, tts_speed * duration / target_duration))
        if abs(adjusted_speed - tts_speed) >= 0.02:
            video_path, log_tail, duration = run_once(adjusted_speed, "square-ui")
            actual_speed = adjusted_speed
            attempts.append({"speed": round(adjusted_speed, 2), "duration": round(duration, 2)})

    return video_path, log_tail, {
        "duration": duration,
        "tts_speed": actual_speed,
        "attempts": attempts,
    }


st.set_page_config(
    page_title="商品短视频测试台",
    page_icon="🎬",
    layout="wide",
)

st.markdown(
    """
    <style>
    .stApp { background: #f7f5f1; color: #24211d; }
    [data-testid="stSidebar"] { background: #f0ece4; }
    .block-container { padding-top: 1.6rem; padding-bottom: 2rem; max-width: 1320px; }
    h1 { letter-spacing: 0 !important; font-size: 2rem !important; }
    h2, h3 { letter-spacing: 0 !important; }
    div[data-testid="stMetricValue"] { font-size: 1.4rem; }
    .status-line {
        padding: 10px 0 12px;
        border-bottom: 1px solid rgba(36, 33, 29, .12);
        color: rgba(36, 33, 29, .72);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("商品短视频测试台")
st.markdown('<div class="status-line">火山方舟看图写分镜，Edge TTS 配音，本地 Pixelle/ffmpeg 合成 1:1 MP4；不强制每段固定秒数，默认目标 14-16 秒。</div>', unsafe_allow_html=True)

generate_script = False
compose = False
run_batch = False
title = "浅鹅黄假两件Polo衫"
category = "T恤/Polo衫"
sample_available = all((SAMPLE_ROOT / "assets" / f"main_{index}.png").exists() for index in range(1, 6))
use_sample = sample_available
uploads = []
excel_upload = None
batch_limit = 3
tail_padding = 0.3
tts_speed = 1.08

with st.sidebar:
    st.subheader("输入")
    api_key = st.text_input("火山方舟 API Key", value=os.environ.get("ARK_API_KEY", ""), type="password")
    model = st.text_input("模型", value=DEFAULT_MODEL)
    tail_padding = st.slider("尾音保护秒数", min_value=0.0, max_value=0.8, value=0.3, step=0.05)
    tts_speed = st.slider("语音速度", min_value=0.85, max_value=1.15, value=1.08, step=0.01)
    mode = st.radio("模式", ["单商品", "Excel批量"], horizontal=True)

    if mode == "单商品":
        title = st.text_input("商品标题", value=title)
        category = st.text_input("类目", value=category)
        use_sample = st.toggle("使用当前样例 5 张图", value=sample_available, disabled=not sample_available)
        if not use_sample:
            uploads = st.file_uploader(
                "上传商品主图",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=True,
            )
        elif not sample_available:
            st.caption("当前容器没有挂载样例图，直接上传图片或使用 Excel 批量模式。")

        generate_script = st.button("生成分镜脚本", type="primary", use_container_width=True)
        compose = st.button("合成 1:1 视频", use_container_width=True)
    else:
        excel_upload = st.file_uploader("上传商品 Excel", type=["xlsx"])
        batch_limit = st.number_input("最多生成商品数", min_value=1, max_value=20, value=3, step=1)
        run_batch = st.button("批量生成视频", type="primary", use_container_width=True)

if "run_dir" not in st.session_state:
    st.session_state.run_dir = None
if "image_paths" not in st.session_state:
    st.session_state.image_paths = []
if "script_payload" not in st.session_state:
    st.session_state.script_payload = None
if "video_path" not in st.session_state:
    st.session_state.video_path = None
if "video_meta" not in st.session_state:
    st.session_state.video_meta = None
if "batch_results" not in st.session_state:
    st.session_state.batch_results = []

batch_products = []
if mode == "Excel批量" and excel_upload is not None:
    try:
        batch_products = extract_products(read_xlsx_rows(excel_upload.getvalue()))
    except Exception as error:
        st.error(f"Excel 解析失败：{error}")

left, right = st.columns([0.95, 1.25], gap="large")

with left:
    if mode == "Excel批量":
        st.subheader("Excel 商品")
        if batch_products:
            st.dataframe(
                [
                    {
                        "行号": product["row"],
                        "商品": product["title"],
                        "类目": product["category"],
                        "图片数": len(product["image_urls"]),
                    }
                    for product in batch_products
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("上传前面那种商品 Excel，至少包含商品编号、商品分类、主图 URL。")

        if st.session_state.batch_results:
            done_count = sum(1 for item in st.session_state.batch_results if item["状态"] == "完成")
            metrics = st.columns(3)
            metrics[0].metric("商品", len(st.session_state.batch_results))
            metrics[1].metric("完成", done_count)
            metrics[2].metric("画幅", "1:1")
    else:
        st.subheader("素材预览")
        preview_paths = [SAMPLE_ROOT / "assets" / f"main_{index}.png" for index in range(1, 6)] if use_sample else []
        if use_sample:
            st.image([str(path) for path in preview_paths], width=136)
        elif uploads:
            st.image(uploads, width=136)
        else:
            st.info("上传 3-6 张商品主图，或打开样例开关。")

        if st.session_state.script_payload:
            usage = st.session_state.script_payload.get("usage") or {}
            metrics = st.columns(3)
            metrics[0].metric("场景", len(st.session_state.script_payload.get("scenes", [])))
            metrics[1].metric("Token", usage.get("total_tokens", "-"))
            metrics[2].metric("画幅", "1:1")

with right:
    st.subheader("结果")
    if mode == "Excel批量":
        if st.session_state.batch_results:
            st.dataframe(st.session_state.batch_results, use_container_width=True, hide_index=True)
            ok_results = [item for item in st.session_state.batch_results if item["状态"] == "完成"]
            if ok_results:
                preview = st.selectbox("预览视频", [item["视频"] for item in ok_results])
                st.video(preview)
        elif batch_products:
            st.info(f"已识别 {len(batch_products)} 个商品，可以批量生成。")
        else:
            st.info("等待 Excel。")
    else:
        if st.session_state.video_path:
            st.video(str(st.session_state.video_path))
            st.caption(str(st.session_state.video_path))
            if st.session_state.video_meta:
                st.caption(
                    f"时长 {st.session_state.video_meta['duration']:.2f}s，"
                    f"语速 {st.session_state.video_meta['tts_speed']:.2f}x"
                )
        elif st.session_state.script_payload:
            st.success("脚本已生成，可以合成视频。")
        else:
            st.info("先生成分镜脚本。")

if generate_script:
    if not api_key:
        st.error("先填写火山方舟 API Key。")
    elif not use_sample and not uploads:
        st.error("请上传商品主图，或使用样例图。")
    else:
        with st.spinner("正在看图并生成分镜脚本..."):
            run_dir, image_paths = prepare_run_dir(title, uploads, use_sample)
            script_payload = call_ark(
                api_key=api_key,
                model=model,
                title=title,
                category=category,
                image_paths=image_paths,
            )
            save_script(run_dir, script_payload)
            st.session_state.run_dir = run_dir
            st.session_state.image_paths = image_paths
            st.session_state.script_payload = script_payload
            st.session_state.video_path = None
        st.rerun()

if compose:
    if not st.session_state.script_payload or not st.session_state.run_dir:
        st.error("先生成分镜脚本。")
    else:
        with st.spinner("正在配音并合成视频..."):
            video_path, _log, video_meta = compose_video(
                st.session_state.run_dir,
                title,
                category,
                st.session_state.image_paths,
                tail_padding,
                tts_speed,
            )
            st.session_state.video_path = video_path
            st.session_state.video_meta = video_meta
        st.rerun()

if run_batch:
    if not api_key:
        st.error("先填写火山方舟 API Key。")
    elif not batch_products:
        st.error("没有从 Excel 里识别到可生成的商品。")
    else:
        selected_products = batch_products[: int(batch_limit)]
        results = []
        progress = st.progress(0)
        status = st.empty()

        for index, product in enumerate(selected_products, 1):
            status.write(f"正在生成第 {index}/{len(selected_products)} 个：{product['title']}")
            try:
                run_dir, image_paths = prepare_product_run(product)
                script_payload = call_ark(
                    api_key=api_key,
                    model=model,
                    title=product["title"],
                    category=product["category"],
                    image_paths=image_paths,
                )
                save_script(run_dir, script_payload)
                video_path, _log, video_meta = compose_video(
                    run_dir,
                    product["title"],
                    product["category"],
                    image_paths,
                    tail_padding,
                    tts_speed,
                )
                usage = script_payload.get("usage") or {}
                results.append(
                    {
                        "行号": product["row"],
                        "商品": product["title"],
                        "状态": "完成",
                        "Token": usage.get("total_tokens", ""),
                        "秒数": f"{video_meta['duration']:.2f}",
                        "语速": f"{video_meta['tts_speed']:.2f}x",
                        "视频": str(video_path),
                        "脚本": str(run_dir / "result" / "ark-multimodal-script.json"),
                    }
                )
            except Exception as error:
                results.append(
                    {
                        "行号": product["row"],
                        "商品": product["title"],
                        "状态": "失败",
                        "Token": "",
                        "视频": "",
                        "脚本": "",
                        "错误": str(error)[-260:],
                    }
                )
            progress.progress(index / len(selected_products))

        st.session_state.batch_results = results
        st.rerun()

if mode == "单商品" and st.session_state.script_payload:
    st.divider()
    st.subheader("分镜脚本")
    st.write(st.session_state.script_payload.get("visual_summary", ""))
    st.dataframe(
        st.session_state.script_payload.get("scenes", []),
        use_container_width=True,
        hide_index=True,
    )
    with st.expander("JSON"):
        st.json(st.session_state.script_payload)
