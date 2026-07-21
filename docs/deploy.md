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
| `DATABASE_URL` | (compose 自动注入) | Postgres 连接串;必填,未配置则应用启动失败 |
| `POSTGRES_PASSWORD` | `dcpass` | docker-compose 内置 PG 服务的密码(`dc` 用户) |
| `DC_DB_AUTO_MIGRATE` | `1` | 启动时自动 `alembic upgrade head`(默认开);设为 `0` 改由运维手动控制 |
| `DC_DB_AUTO_CREATE` | 空 | 设为 `1` 时跳过 alembic 直接 `CREATE TABLE IF NOT EXISTS`(仅测试用) |

> LLM / OCR 配置(API Base、API Key、模型名、超时、并发等)统一持久化于 Postgres 的 `llm_config` 表,通过 UI 设置页维护,不使用环境变量。首次启动若 `./data/llm_config.json` 存在且 PG 无记录,会自动一次性导入该文件并保留文件作备份,之后不再读取。

## 数据持久化

部署涉及两个数据目录,均在宿主 `./data/` 下:

| 路径 | 内容 | 容器内路径 |
|------|------|------------|
| `./data/uploads` | 上传的源/目标文件(docx/pdf) | `/app/.dc_data/uploads` |
| `./data/logs/app.log` | 应用滚动日志(10MB×5) | `/app/.dc_data/logs/app.log` |
| `./data/pg/` | Postgres 数据目录(任务记录、里程碑事件、**完整报告 JSONB**、**LLM/OCR 模型配置**) | `/var/lib/postgresql/data` |

> `./data/llm_config.json`(旧版 LLM 配置文件)在升级后不再被读取,首次启动会自动导入 PG 一次;若文件存在,作为备份保留。

**报告持久化**:比对报告 JSON、任务元数据、里程碑事件**只**写入 Postgres,不再落 `.dc_data/reports/` 文件。查询/下载端点先查内存,未命中再从 PG JSONB 还原。上传的原始 docx/pdf 仍保存在文件系统(供 `/source`、`/docx-preview`、PDF/DOCX 标注报告生成读取)。

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
