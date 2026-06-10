# Pixelle-Video 本地协作规则

## 商品短视频接口服务

- 本仓库包含原 Pixelle-Video 项目，以及本地二次开发的商品短视频接口服务。
- 商品短视频接口入口在 `api/routers/product_videos.py`，后台任务逻辑在 `api/product_video_jobs.py`。
- 对外接口前缀固定为 `/api/product-videos`。
- 线上 Docker 服务监听容器内 `0.0.0.0:8000`，1Panel 映射为 `30005:8000`。
- 线上镜像构建文件是 `Dockerfile.product-api`，compose 文件是 `docker-compose.product-api.yml`。
- Mac 本地构建给 1Panel 服务器使用时，必须确认目标平台是 `linux/amd64`；不要默认交付 Apple Silicon 的 `arm64` 镜像。

## 密钥和部署

- 仓库内不得写入真实 `ARK_API_KEY`、`INTERNAL_API_TOKEN`、OSS AccessKey、1Panel 密码或其他密钥。
- `docker-compose.product-api.yml` 只能从服务器 `.env` 读取 `ARK_API_KEY`、`INTERNAL_API_TOKEN`、`OSS_*` 和 `PIM_*` 环境变量。
- 桌面上的产品/研发交接文档可以临时写明真实地址和测试 token；仓库 docs 只能写变量名和接口规格。
- `/hlg` 是 1Panel 面板入口，不是业务接口路径；业务接口跑在服务根路径。
- 2026-06-08 起，GPU 机器 `172.16.2.203` 使用 PM2 部署在 `/service/pixelle-video-product-api`；服务器上的 `.env`、`run_product_api.sh`、数据目录和 PM2 配置是远端运行态文件，不要从本地 rsync 覆盖或删除。
- 2026-06-10 起，PIM 批量生产主链路只需要 `pixelle-video-pim-worker` 常驻运行；`pixelle-video-product-api` 是手工接口联调服务，除非明确要测 `/api/product-videos`，否则在 `172.16.2.203` 上保持停止。
- 同步代码到远端时必须排除 `.env`、`run_product_api.sh`、`.codex-bin/`、`.codex_py_logs/`、`.venv/`、`output/`、`data/` 和 `/service/pixelle-video-product-api-data/`。
- `.codex-bin/` 只允许作为本地临时工具目录，不能进入 Docker build context 或远端服务器；它可能包含 macOS/arm64 ffmpeg 链接，放到 Linux 服务器会抢占系统 `ffmpeg/ffprobe` 并导致生成失败。

## 版本管理

- 2026-06-09 起，本项目默认 GitHub 仓库为 `git@github.com:caomei242/piliangshengchengshipin.git`，对应页面是 `https://github.com/caomei242/piliangshengchengshipin`。
- 本地 `origin` 指向上述业务仓库，日常提交使用 `git push` 推送到 `origin/main`。
- 原 Pixelle 上游保留为 `upstream=https://github.com/AIDC-AI/Pixelle-Video.git`，只用于必要时查看或对比原项目，不作为默认推送目标。
- 远端 `origin/main` 是 2026-06-09 用当前代码树做的干净初始提交，不带原 Pixelle 浅克隆历史；本地备份分支 `backup/shallow-upstream-before-publish` 保留了推送前的浅历史提交。
- 提交前必须做密钥扫描，确认没有真实 Ark、OSS、1Panel、PIM 或内部 token 进入 Git。
- 2026-06-10 起，凡是涉及代码改动，完成验证后必须提交并推送到 `origin/main`，避免线上或本地变更无法回滚。

## 接口约定

- `POST /api/product-videos/jobs` 必须鉴权，并使用 `Idempotency-Key` 或请求体 `request_id` 做幂等。
- `GET /api/product-videos/jobs/{job_id}` 用于研发轮询整批任务状态。
- `GET /api/product-videos/items/{item_id}/video` 和 `/script` 也必须鉴权。
- 商品视频默认规格为 1:1，目标时长为 14-16 秒。
- 商品图优先读取结构化 `source_images`，兼容旧 `image_urls`；`source_images` 只允许商品主图参与生成，SKU 图、规格图、颜色图、详情图、评价图等非主图必须跳过；可用主图少于 `scene_count` 时商品失败，错误为 `source_images_insufficient`，不要复制或复用图片补位。
- 生成成功后优先上传 OSS；`video_url` / `script_url` 优先返回 OSS 地址，`download_url` / `script_download_url` 只作为本地调试下载兜底。
- 一个商品失败不影响同批次其他商品继续生成。
- 当前默认并发先设 4：`PRODUCT_VIDEO_MAX_CONCURRENCY=4`，PIM worker 默认 `PIM_WORKER_CONCURRENCY=4`；`PIM_WORKER_ONCE=1` 联调时保持单任务，避免误领多条。
- PIM worker 必须常驻轮询；有任务就连续领取和处理，队列为空时默认每 5 秒查询一次。PIM worker 直接完成下载主图、生成脚本、合成 MP4、上传 OSS、回传 PIM，不再依赖本地 `/api/product-videos` API 服务。
- 服务启动时必须自动检测 CUDA/NVIDIA GPU；系统 `ffmpeg` 能看到 `h264_nvenc` 时，视频编码走 NVENC，默认 `PIXELLE_NVENC_PRESET=medium` 兼容 Ubuntu 20.04 的 ffmpeg 4.2。Chromium 帧渲染默认 CPU 稳定模式，只有显式设置 `PIXELLE_CHROMIUM_GPU=on` 时才尝试 GPU。
- 批量生产对接 PIM 现有接口，不要求 PIM 调用我们的视频服务来创建批量任务：worker 调 `GET /api/video-tool/get?type=1` 取 1 个任务，生成后调 `POST /api/video-tool/submit` 回传 `status=2/3`。
- PIM `status=3` 的 `error_msg` 会给客户看，必须是中文短句；英文错误码、Python 堆栈、内部路径只允许写 worker 日志，不允许直接回传给 PIM。
- PIM 环境地址：dev `https://gdpim-dev.huanleguang.com`，stage `https://gdpim-stage.huanleguang.com`，prod `https://gdpim.huanleguang.com`。
- PIM 侧负责生成中状态和超时重置：每 30 分钟将生成中超过 1 小时未更新任务重置为等待生成；worker 不做心跳或续租。

## 文档边界

- 给产品或外部研发看的傻瓜版对接文档在 `/Users/gd/Desktop/商品短视频生成服务-产品对接说明.md`。
- 仓库内长期接口文档在 `docs/zh/reference/product-video-product-brief.md` 和 `docs/zh/reference/api-overview.md`。
- 架构和状态流转说明在 `docs/zh/development/architecture.md`。
