# ─────────────────────────────────────────────────────────────
# 文档比对系统 — 单容器镜像(后端 FastAPI + 前端 Vue SPA)
# 目标平台:linux/amd64
# ─────────────────────────────────────────────────────────────

# ── Stage 1: 构建前端 ──
FROM node:20-alpine AS frontend-build

WORKDIR /build

# 先拷依赖清单,利用 Docker 层缓存
COPY web/package.json web/package-lock.json ./
RUN npm ci

# 拷源码并构建
COPY web/ ./
RUN npm run build


# ── Stage 2: Python 运行时 ──
FROM python:3.12-slim AS runtime

# 国内 pip 镜像:避免构建隔离拉 setuptools 时 pypi.org SSL 超时
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com

# 系统依赖:PyMuPDF / PaddleOCR(OpenCV libGL) 运行库 + LibreOffice 无头 DOCX→PDF
# 渲染(原件侧标注) + 开源 CJK/Office 兼容字体 + curl(健康检查)+ tzdata(时区)。
# 等线、微软雅黑、Arial 等受授权约束的字体不打进镜像；部署时可只读挂载到
# /usr/local/share/fonts/authorized，由 Fontconfig 优先匹配。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl libgl1 tzdata libreoffice-writer fontconfig \
        fonts-noto-cjk fonts-liberation2 \
        fonts-crosextra-carlito fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/*

# Word 常用字体的稳定回退顺序。真实授权字体若由部署环境挂载，会保留在
# 请求族名前并优先命中；本配置只为缺失字体提供开源替代。
COPY docker/fonts/25-document-comparison.conf /etc/fonts/conf.d/25-document-comparison.conf
RUN fc-cache -f

WORKDIR /app

# 安装 Python 依赖(先拷 pyproject,利用层缓存)
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    "gunicorn>=21.0" \
    "python-multipart>=0.0.9" \
    "pydantic>=2.6" \
    "python-docx>=1.1" \
    "PyMuPDF>=1.24" \
    "httpx>=0.27" \
    "requests>=2.31" \
    "paddleocr>=3.7,<3.8" \
    "SQLAlchemy>=2.0" \
    "psycopg[binary]>=3.1" \
    "alembic>=1.13"

# opencv-contrib-python 还依赖 GLib 线程库(libgthread-2.0.so.0)。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 拷后端源码
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

# 拷 Alembic 迁移脚本(启动期自动 alembic upgrade head 需要)
COPY alembic.ini ./
COPY alembic/ ./alembic/

# 拷 gunicorn 启动配置(CMD 引用)
COPY gunicorn.conf.py ./

# 拷前端构建产物
COPY --from=frontend-build /build/dist ./static

# 持久化数据卷
RUN mkdir -p /app/.dc_data
VOLUME ["/app/.dc_data"]

# 应用版本号:构建期烧入镜像 ARG → ENV,使任意机器拉取镜像后无需再传 DC_VERSION 即可自报
# 正确版本(版本是镜像的固有属性,不应由部署环境的 .env 决定或覆盖)。
# docker compose 通过 build.args 传入(取自构建机 .env 的 DC_VERSION,与镜像 tag 同源);
# 未传时为空 → 运行时回退到 package __version__(__init__.py)。
ARG DC_VERSION=""

ENV DC_HOST=0.0.0.0 \
    DC_PORT=8000 \
    DC_STORAGE_DIR=/app/.dc_data \
    DC_STATIC_DIR=/app/static \
    DC_DOCX_RENDERER_PATH=/usr/bin/soffice \
    TZ=Asia/Shanghai \
    DC_VERSION=${DC_VERSION}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fs http://localhost:8000/health || exit 1

# gunicorn + uvicorn worker,worker 数由 DC_UVICORN_WORKERS 控制(默认 1)。
# 本地开发可用 `python -m document_comparison.api.app`(单进程 uvicorn)。
CMD ["gunicorn", "-c", "gunicorn.conf.py", "document_comparison.api.app:app"]
