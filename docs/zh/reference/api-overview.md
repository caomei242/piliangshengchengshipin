# API 概览

Pixelle-Video 提供 Python SDK 和 HTTP REST API 两种方式。

---

## Python SDK

### PixelleVideoCore

主要服务类，提供视频生成功能。

```python
from pixelle_video.service import PixelleVideoCore

pixelle = PixelleVideoCore()
await pixelle.initialize()
```

### generate_video()

生成视频的主要方法。

**参数**:

- `text` (str): 主题或完整文案
- `mode` (str): 生成模式 ("generate" 或 "fixed")
- `n_scenes` (int): 分镜数量
- `title` (str, optional): 视频标题
- `tts_workflow` (str): TTS 工作流
- `media_workflow` (str): 媒体生成工作流（图像或视频）
- `frame_template` (str): 视频模板
- `template_params` (dict, optional): 模板自定义参数
- `bgm_path` (str, optional): BGM 文件路径
- `bgm_volume` (float): BGM 音量 (0.0-1.0)

**返回**: `VideoResult` 对象

---

## HTTP REST API

启动 API 服务器：

```bash
uv run uvicorn api.app:app --host 0.0.0.0 --port 8000
```

### 视频生成 - 同步

`POST /api/video/generate/sync`

同步生成视频，等待完成后返回结果。适合小视频（< 30 秒）。

**请求体**:

```json
{
  "text": "为什么要养成阅读习惯",
  "mode": "generate",
  "n_scenes": 5,
  "frame_template": "1080x1920/image_default.html",
  "template_params": {
    "accent_color": "#3498db",
    "background": "https://example.com/custom-bg.jpg"
  },
  "title": "阅读的力量"
}
```

**响应**:

```json
{
  "success": true,
  "message": "Success",
  "video_url": "http://localhost:8000/api/files/xxx/final.mp4",
  "duration": 45.5,
  "file_size": 12345678
}
```

### 视频生成 - 异步

`POST /api/video/generate/async`

异步生成视频，立即返回任务 ID。适合大视频。

**响应**:

```json
{
  "success": true,
  "message": "Task created successfully",
  "task_id": "abc123"
}
```

### 查询任务状态

`GET /api/tasks/{task_id}`

**响应**:

```json
{
  "task_id": "abc123",
  "status": "completed",
  "result": {
    "video_url": "http://localhost:8000/api/files/xxx/final.mp4",
    "duration": 45.5,
    "file_size": 12345678
  }
}
```

### 商品短视频任务接口

`POST /api/product-videos/jobs`

按商品信息和商品主图 URL 创建 1:1 商品短视频生成任务。该接口面向内部研发系统，必须携带内部 token：

```http
Authorization: Bearer <INTERNAL_API_TOKEN>
Idempotency-Key: <业务唯一请求号>
```

幂等规则：优先使用 Header `Idempotency-Key`，没有 Header 时使用请求体 `request_id`。同一个幂等键重复提交会返回已有 `job_id`，不会重复创建任务。

最小请求体：

```json
{
  "request_id": "req-20260602-0001",
  "source": "commerce-system",
  "options": {
    "aspect_ratio": "1:1",
    "target_duration_seconds": [14, 16],
    "scene_count": 5
  },
  "products": [
    {
      "external_product_id": "3814460745354183082",
      "title": "浅鹅黄假两件Polo衫",
      "category": "T恤/Polo衫",
      "source_images": [
        {"image_id": "img-001", "position": "main_1", "image_type": "main", "url": "https://example.com/main_1.jpg", "width": 1200, "height": 1200},
        {"image_id": "img-002", "position": "main_2", "image_type": "main", "url": "https://example.com/main_2.jpg", "width": 1200, "height": 1200},
        {"image_id": "img-003", "position": "main_3", "image_type": "main", "url": "https://example.com/main_3.jpg", "width": 1200, "height": 1200},
        {"image_id": "img-004", "position": "main_4", "image_type": "main", "url": "https://example.com/main_4.jpg", "width": 1200, "height": 1200},
        {"image_id": "img-005", "position": "main_5", "image_type": "main", "url": "https://example.com/main_5.jpg", "width": 1200, "height": 1200}
      ]
    }
  ]
}
```

相关接口：

- `GET /api/product-videos/jobs/{job_id}`：查询整批任务状态。
- `GET /api/product-videos/jobs/{job_id}/items/{item_id}`：查询单个商品状态。
- `GET /api/product-videos/items/{item_id}/video`：本地调试下载 MP4，正式结果优先用 `video_url` 的 OSS 地址。
- `GET /api/product-videos/items/{item_id}/script`：下载分镜脚本。

结果口径：

- `video_url` / `script_url`：优先返回 OSS 地址；未配置 OSS 时返回服务本地下载地址。
- `download_url` / `script_download_url`：服务本地调试下载地址。
- `oss_key` / `script_oss_key`：OSS 对象 key。
- `storage`：OSS 上传结果和对象元数据；未配置 OSS 时为 `{ "enabled": false, ... }`。
- `selected_images`：实际用于生成的主图清单。
- `source_images` 只允许商品主图参与生成，`image_type` 建议传 `main`，`position` 建议传 `main_1`、`main_2` 等；SKU 图、规格图、颜色图、详情图、评价图等非主图不计入可用主图数。
- 可用主图少于 `scene_count` 时该商品失败，回传 `source_images_insufficient`，不复制图片补位。

部署环境变量：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `ARK_API_KEY` | 是 | 火山方舟多模态模型调用密钥，只放在服务端 |
| `INTERNAL_API_TOKEN` | 是 | 内部研发系统访问商品短视频接口的 token |
| `PIXELLE_RUNS_ROOT` | 否 | 商品视频运行目录 |
| `PRODUCT_VIDEO_STATE_ROOT` | 否 | 任务状态持久化目录 |
| `PRODUCT_VIDEO_MAX_CONCURRENCY` | 否 | 商品生成并发数，默认 4 |
| `PIXELLE_USE_CUDA` | 否 | CUDA 检测开关，默认 `auto`；检测到 CUDA 时启用 GPU 路径 |
| `PIXELLE_FFMPEG_ENCODER` | 否 | ffmpeg 编码器选择，默认 `auto`；CUDA + `h264_nvenc` 可用时使用 NVENC |
| `PIXELLE_NVENC_PRESET` | 否 | NVENC preset，默认 `medium`，兼容 Ubuntu 20.04 ffmpeg 4.2；新 ffmpeg 可按需改成 `p4` 等新版 preset |
| `PIXELLE_CHROMIUM_GPU` | 否 | Chromium 帧渲染 GPU 开关，默认 `off`；稳定生成优先 CPU，显式 `on` 才尝试 GPU |
| `OSS_ACCESS_KEY_ID` | 否 | OSS AccessKey ID，只放在服务端运行环境 |
| `OSS_ACCESS_KEY_SECRET` | 否 | OSS AccessKey Secret，只放在服务端运行环境 |
| `OSS_BUCKET` | 否 | OSS bucket，默认 `hlg-team` |
| `OSS_ENDPOINT` | 否 | OSS 外网 endpoint，默认 `oss-cn-zhangjiakou.aliyuncs.com` |
| `OSS_INTERNAL_ENDPOINT` | 否 | OSS 内网 endpoint，默认 `oss-cn-zhangjiakou-internal.aliyuncs.com` |
| `OSS_USE_INTERNAL_ENDPOINT` | 否 | 是否上传时使用内网 endpoint，默认 `false` |
| `OSS_PUBLIC_BASE_URL` | 否 | 返回给研发保存的公开访问域名，不填则用 bucket 域名 |
| `OSS_PREFIX` | 否 | OSS 对象前缀，默认 `ai批量生产视频/` |
| `OSS_URL_MODE` | 否 | `signed` 或 `public`，默认 `signed` |
| `OSS_SIGNED_URL_EXPIRES_SECONDS` | 否 | 签名 URL 有效期，默认 604800 秒 |

### PIM Worker 对接

批量生产时不要求 PIM 调用商品短视频服务新增接口，而是由视频 worker 主动调用 PIM 现有接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/video-tool/get?type=1` | 取一个 AI 生成视频待处理任务 |
| `POST /api/video-tool/submit` | 上报生成结果 |

PIM 环境地址：

| 环境 | 地址 |
| --- | --- |
| dev | `https://gdpim-dev.huanleguang.com` |
| stage | `https://gdpim-stage.huanleguang.com` |
| prod | `https://gdpim.huanleguang.com` |

worker 收到 PIM task 后，将 `product.image_urls` 转成 `source_images[].image_type=main` 后传入本地 `/api/product-videos/jobs`。生成成功后回传：

```json
{
  "id": 1001,
  "status": 2,
  "video_url": "https://...oss.../generated.mp4",
  "error_msg": ""
}
```

生成失败后回传：

```json
{
  "id": 1001,
  "status": 3,
  "video_url": "",
  "error_msg": "source_images_insufficient: 可用商品主图少于 scene_count"
}
```

PIM 队列为空时可能返回 `{ "code": 500, "message": "没有需要生成的任务" }`，worker 按配置间隔重试即可。PIM 侧每 30 分钟会将生成中超过 1 小时未更新的任务重置为等待生成，worker 不做心跳或续租。

---

## 请求参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | 是 | 主题或完整文案 |
| `mode` | string | 否 | `"generate"` (AI 生成) 或 `"fixed"` (固定文案) |
| `n_scenes` | int | 否 | 分镜数量 (1-20)，仅 generate 模式有效 |
| `title` | string | 否 | 视频标题（不填则自动生成） |
| `frame_template` | string | 否 | 模板路径，如 `1080x1920/image_default.html` |
| `template_params` | object | 否 | 模板自定义参数（颜色、背景等） |
| `media_workflow` | string | 否 | 媒体工作流（图像或视频生成） |
| `tts_workflow` | string | 否 | TTS 工作流 |
| `ref_audio` | string | 否 | 声音克隆参考音频路径 |
| `prompt_prefix` | string | 否 | 图像风格前缀 |
| `bgm_path` | string | 否 | BGM 文件路径 |
| `bgm_volume` | float | 否 | BGM 音量 (0.0-1.0，默认 0.3) |

---

## 更多信息

API 文档也可通过 Swagger UI 访问：`http://localhost:8000/docs`
