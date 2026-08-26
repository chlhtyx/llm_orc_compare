# Docker 部署指南

## 目标环境

Linux x86_64(amd64)、Docker 20.10+、Docker Compose v2。

## 快速开始

```bash
# 1. 进入项目目录
cd document_comparison

# 2. 准备环境变量
cp .env.example .env

# 3. 构建并启动(会同时拉起 postgres 服务,应用依赖 PG 健康检查通过后启动)
docker compose up -d --build

# 4. 检查状态
docker compose ps
curl http://localhost:8000/health
```

浏览器打开 `http://<服务器IP>:8000` 即可访问。

Postgres 数据持久化在宿主 `./data/pg/`,应用依赖 `pg_isready` 健康检查通过后再启动,因此首次启动稍慢(等 PG 就绪)。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DC_PORT` | `8000` | 宿主机映射端口(容器内固定 8000) |
| `DC_FONTS_DIR` | `./fonts` | 可选的授权字体目录，只读挂载到 `/usr/local/share/fonts/authorized`；不要把字体提交到仓库或打进镜像 |
| `DATABASE_URL` | (compose 自动注入) | Postgres 连接串;必填,未配置则应用启动失败 |
| `POSTGRES_PASSWORD` | `dcpass` | docker-compose 内置 PG 服务的密码(`dc` 用户) |
| `DC_DB_AUTO_MIGRATE` | `1` | 启动时自动 `alembic upgrade head`(默认开);设为 `0` 改由运维手动控制 |
| `DC_DB_AUTO_CREATE` | 空 | 设为 `1` 时跳过 alembic 直接 `CREATE TABLE IF NOT EXISTS`(仅测试用) |
| `DC_EXTERNAL_API_KEY` | 空 | 外部 API Key 的可选启动默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_PUBLIC_BASE_URL` | 空 | 服务公开地址的可选启动默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_MAX_UPLOAD_MB` | `50` | 外部接口单文件上限默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_IMAGE_DPI` | `144` | 全页高亮 PNG DPI 默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_OCR_BACKEND` | `paddleocr` | 外部 API 与页面测试共用的 OCR 引擎默认值 |
| `DC_EXTERNAL_ENABLE_LLM_ALIGNMENT` | `0` | 外部 API 是否默认启用 LLM 原始块联合分段对齐；失败自动回退 Clause 对齐 |
| `DC_EXTERNAL_ENABLE_LLM_JUDGE` | `0` | 外部 API 是否默认启用 LLM 辅助说明 |
| `DC_EXTERNAL_ENABLE_RISK_ASSESSMENT` | `0` | 外部 API 是否默认开启风险分级 |
| `DC_CONSOLE_PASSWORD` | 空 | Web 控制台访问口令；非空时控制台业务接口需口令登录(会话 Cookie),外部 X-API-Key 接口、`/health`、`/api/v1/version` 与静态资源不受影响 |
| `DC_CONSOLE_SESSION_TTL_HOURS` | `12` | 控制台会话有效期(小时)；修改口令会使所有已发会话立即失效 |

> LLM / OCR 配置(API Base、API Key、模型名、超时、并发等)统一持久化于 Postgres 的 `llm_config` 表,通过 UI 设置页维护,不使用环境变量。首次启动若 `./data/llm_config.json` 存在且 PG 无记录,会自动一次性导入该文件并保留文件作备份,之后不再读取。

外部系统 API 配置在管理端“合同比对 API”页维护并写入 Postgres，保存后立即生效。
上述 `DC_EXTERNAL_*` 仅作为首次启动或数据库配置不可用时的默认值。该页中的管线
测试与正式外部接口共用 OCR/LLM 比对选项，会生成真实 OCR/比对调用和全页 PNG，
但不会发送回调。测试文件与产物按普通任务保存在 `uploads/`、`reports/` 和任务历史中。

## DOCX 渲染字体

镜像内置 Noto CJK、Liberation、Carlito、Caladea，并通过 Fontconfig 为常见 Word 字体提供回退。若合同依赖等线、微软雅黑、宋体、Arial 等授权字体，请在部署机准备只包含授权 `.ttf`、`.ttc`、`.otf` 文件的目录，在 `.env` 中设置 `DC_FONTS_DIR=/srv/document-comparison/fonts`，然后重新部署：

```bash
docker compose up -d --build
docker compose exec llm-ocr-compare fc-match "等线"
docker compose exec llm-ocr-compare fc-match Arial
```

挂载目录为只读，不会写入上传文件、报告、日志或镜像。字体改变只影响后续 DOCX 派生 PDF；既有 HTML/PNG 报告需重新提交任务生成。

## 数据持久化

部署涉及两个数据目录,均在宿主 `./data/` 下:

| 路径 | 内容 | 容器内路径 |
|------|------|------------|
| `./data/uploads` | 上传的源/目标文件(docx/pdf),按 `uploads/{task_id}/` 子目录归集;旧版本的平铺命名文件仍可直接读取,无需迁移 | `/app/.dc_data/uploads` |
| `./data/reports` | 外部接口的标注 PDF 与逐页高亮 PNG | `/app/.dc_data/reports` |
| `./data/logs/app.log` | 应用滚动日志(10MB×5) | `/app/.dc_data/logs/app.log` |
| `./data/pg/` | Postgres 数据目录(任务记录、里程碑事件、**完整报告 JSONB**、**LLM/OCR 模型配置**) | `/var/lib/postgresql/data` |

> `./data/llm_config.json`(旧版 LLM 配置文件)在升级后不再被读取,首次启动会自动导入 PG 一次;若文件存在,作为备份保留。

**报告持久化**:比对报告 JSON、任务元数据、里程碑事件写入 Postgres。上传的原始 docx/pdf 保存在文件系统；外部 API 还会把标注 PDF 和逐页 PNG 写入 `.dc_data/reports/`，因此备份时必须同时保留该目录。

> 升级提示:从老版本(报告写文件)升级后,老报告 `.json` 文件不再被读取;如需保留历史可访问性,升级前用一次性脚本把它们导入 PG(`db_repo.save_compare_report` 等)。

备份(应用文件 + 数据库):

备份(应用文件 + 数据库):

    # 应用文件(volume 绑定方式:直接 tar 宿主目录即可)
    tar czf dc_data_backup.tar.gz -C ./data .

    # Postgres:逻辑备份(推荐,跨版本安全)
    docker compose exec postgres pg_dump -U dc doc_compare > db_backup.sql

恢复:

    tar xzf dc_data_backup.tar.gz -C ./data
    docker compose exec -T postgres psql -U dc doc_compare < db_backup.sql

## 日常运维

    # 查看日志
    docker compose logs -f

    # 重启
    docker compose restart

    # 更新代码后重新部署
    docker compose up -d --build

    # 停止 / 删除
    docker compose down            # 仅停止,保留数据
    docker compose down -v         # 停止并删除数据卷(谨慎!)

## 数据库迁移(Alembic)

应用启动时**默认自动执行** `alembic upgrade head`(幂等),`docker compose up` 即用、无需手动跑迁移。如需关闭(交由 CI/运维手动控制),设置 `DC_DB_AUTO_MIGRATE=0`。

升级或排查 schema 版本时,可手动执行:

    # 进入应用容器执行 alembic
    docker compose exec llm-ocr-compare alembic upgrade head

    # 查看当前版本
    docker compose exec llm-ocr-compare alembic current

开发期可设置 `DC_DB_AUTO_CREATE=1`(优先级高于 `DC_DB_AUTO_MIGRATE`),让应用启动时直接 `CREATE TABLE IF NOT EXISTS`,跳过 alembic 子进程,便于本地快速起服务或跑测试。

## 使用真实 OCR 引擎

默认 `mock` 后端不调用任何外部服务。要启用真实 OCR:

1. 打开 UI 的"设置"页面
2. 配置 API Base、API Key、模型名指向你的推理服务(兼容 OpenAI 协议)
3. 设置页的配置会持久化到 Postgres 的 `llm_config` 表,容器重启后仍然生效

## 反向代理(Nginx)

如需 HTTPS 或自定义域名,在前面加 Nginx:

    server {
        listen 80;
        server_name your-domain.com;

        client_max_body_size 100m;

        location / {
            proxy_pass http://127.0.0.1:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
