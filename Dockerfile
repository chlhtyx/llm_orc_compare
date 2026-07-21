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

# 系统依赖:PyMuPDF 运行库 + curl(健康检查)+ tzdata(时区,日志/时间戳用本地时区)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖(先拷 pyproject,利用层缓存)
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.110" \
    "uvicorn[standard]>=0.27" \
    "python-multipart>=0.0.9" \
    "pydantic>=2.6" \
    "python-docx>=1.1" \
    "PyMuPDF>=1.24" \
    "httpx>=0.27" \
    "SQLAlchemy>=2.0" \
    "psycopg[binary]>=3.1" \
    "alembic>=1.13"

# 拷后端源码
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

# 拷 Alembic 迁移脚本(启动期自动 alembic upgrade head 需要)
COPY alembic.ini ./
COPY alembic/ ./alembic/

# 拷前端构建产物
COPY --from=frontend-build /build/dist ./static

# 持久化数据卷
RUN mkdir -p /app/.dc_data
VOLUME ["/app/.dc_data"]

ENV DC_HOST=0.0.0.0 \
    DC_PORT=8000 \
    DC_STORAGE_DIR=/app/.dc_data \
    DC_STATIC_DIR=/app/static \
    TZ=Asia/Shanghai

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fs http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "document_comparison.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000"]
