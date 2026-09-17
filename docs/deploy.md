# 单服务部署与运维

本文是部署、升级和回滚的统一说明。Vue 前端和 FastAPI 后端运行在同一个应用容器，PostgreSQL 单独运行；OCR/LLM 在控制台配置为外部服务。

## 1. 首次启动

目标环境为 Linux x86_64、Docker 和 Compose v2。在包含 `docker-compose.yml` 的项目目录执行：

```bash
cp .env.example .env
chmod 600 .env
# 编辑 .env：设置 DC_VERSION、POSTGRES_PASSWORD 和 DC_CONSOLE_PASSWORD
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -fsS http://localhost:3012/health
curl -fsS http://localhost:3012/ready
```

已有发布镜像时，可用以下命令代替源码构建：

```bash
docker compose pull llm-ocr-compare
docker compose up -d
```

默认访问 <http://localhost:3012>。修改 `DC_PORT` 后，请同步调整检查命令和 Nginx 上游端口。
应用等待 PostgreSQL 健康检查通过，随后自动执行 `alembic upgrade head`。

登录控制台后，在“设置”配置 OCR/LLM 地址、模型和密钥，在“外部 API 配置”维护外部接口鉴权与公开地址。模型配置保存在 PostgreSQL 中，重启不会丢失。

## 2. 部署配置

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `DC_VERSION` | 见 `.env.example` | 已发布镜像 tag 或源码构建版本；按本次目标版本设置 |
| `DC_PORT` | `3012` | 应用宿主端口，映射到容器内 8000 |
| `COMPOSE_PROJECT_NAME` | `llm-ocr-compare` | Compose 项目名，部署后保持稳定 |
| `POSTGRES_PASSWORD` | `dcpass` | 首次初始化数据库的口令，生产部署前替换 |
| `DC_CONSOLE_PASSWORD` | 空 | 非空时启用控制台登录 |
| `DC_CONSOLE_SESSION_TTL_HOURS` | `12` | 控制台会话有效期，单位小时 |
| `DC_DB_PORT` | `15433` | PostgreSQL 宿主映射端口，仅供受限运维访问 |
| `DC_DB_AUTO_MIGRATE` | `1` | 应用启动时自动迁移；仅在自行管理迁移时改为 `0` |
| `DC_FONTS_DIR` | `./fonts` | 授权字体目录，只读挂载 |
| `DC_UVICORN_WORKERS` | `1` | 应用 worker 数 |
| `DC_MAX_CONCURRENT_TASKS` | `4` | 执行并发上限 |
| `DC_MAX_QUEUED_TASKS` | `50` | 等待队列容量 |

无需配置部署 ID、初始发布状态或双服务参数。Compose 自动注入 `DATABASE_URL`；只有脱离 Compose 运行后端时才需要自行提供连接串。不要在已有数据库上仅修改 `POSTGRES_PASSWORD`：初始化变量不会同步修改库内用户口令。

更多业务配置及并发说明见 [README](../README.md)。控制台保存的模型和外部接口配置不需要复制到 `.env`。

## 3. 日常启动和升级

已有本地镜像时：

```bash
docker compose up -d
```

从镜像仓库升级：先修改 `.env` 的 `DC_VERSION`，再执行：

```bash
docker compose pull llm-ocr-compare
docker compose up -d
```

从当前源码构建升级：

```bash
docker compose up -d --build
```

`up -d` 不会把源码变更打入已有镜像。升级重建同一个应用容器，保留数据目录、端口和业务调用地址。
启动会自动更新版本记录；原状态为 `SERVING` 时继续接单，无需先执行发布控制命令。

显式设置的维护状态会保留：`DRAINING` 拒绝新写请求并继续完成已有队列；`STOPPED` 同时暂停领取任务。两种状态下查询和下载仍可使用，新写请求返回 503 和 `Retry-After`。

`/health` 只检查进程存活；`/ready` 返回 200 且 `ready=true` 表示数据库、迁移、存储、调度器和接单状态均就绪。

## 4. 可选：长任务的排空升级与回滚

存在长任务或需要受控回滚时，建议先排空再重建。Gunicorn 优雅退出等待 120 秒，Compose 等待 130 秒，超过等待时间的任务可能被中断。以下命令中的容器名使用默认值；自定义后请相应替换。

```bash
# 暂停提交 → 等待已有任务及回调完成 → 保存配置快照 → 暂停执行
bash scripts/release.sh prepare llm-ocr-compare before-upgrade 1800
```

任何一步失败都会停止后续操作。排空超时会保持 `DRAINING`，用 `release status` 检查任务、回调及在途操作；需要取消升级时可执行 `release serve`。配置快照包含模型密钥，权限为 0600，只应由运维保管。

排空后按第 5 节备份，修改 `.env` 中的镜像版本，再执行：

```bash
docker compose pull llm-ocr-compare
docker compose up -d llm-ocr-compare
bash scripts/release.sh ctl llm-ocr-compare ready
bash scripts/release.sh ctl llm-ocr-compare serve
curl -fsS http://localhost:3012/ready
```

维护期间 HTTP `/ready` 返回 503 属预期；CLI `release ready` 检查数据库、迁移和存储，成功后再 `serve`。
发布记录供同一服务的 worker 共享，不是容器互斥锁，不要同时运行两个不同版本连接同一业务库。

回滚同样替换原容器。已经恢复接单时，先重新执行 `prepare`，再将 `DC_VERSION` 改回已保存的旧版本，重建并检查后恢复接单。若数据库迁移或模型配置不兼容旧版本，应先评估兼容性，不能仅凭旧镜像可启动就判断回滚成功。

需要恢复模型配置快照时，在服务停止接单且全库排空后执行：

```bash
bash scripts/release.sh ctl llm-ocr-compare restore \
  --file /app/.dc_data/release-snapshots/SNAPSHOT_ID.json \
  --sha256 SNAPSHOT_SHA256
```

路径和 SHA256 使用 `prepare` 的实际输出。恢复命令校验摘要及快照来源，只恢复模型配置，不恢复运行环境变量或整个业务库。不要用上线前的数据库备份覆盖已经接收新任务的业务库来做常规回滚。

## 5. 数据与备份

| 宿主路径 | 内容 |
| --- | --- |
| `data/pg/` | PostgreSQL 数据：任务、完整报告 JSONB、模型配置及审计 |
| `data/uploads/` | 上传原件和回收件 |
| `data/reports/` | PDF、HTML、高亮图片等文件产物 |
| `data/logs/` | 应用日志 |
| `data/release-snapshots/` | 可选发布流程生成的配置快照 |

升级时保留 `data/`。数据库和文件需要一起备份；运行中的 `data/pg/` 直接打包不能替代一致的数据库备份。
以下示例在排空后执行，备份存放在业务目录之外：

```bash
mkdir -p backups
chmod 700 backups
umask 077
# 停止应用以冻结业务写入，PostgreSQL 保持运行
docker compose stop llm-ocr-compare
docker compose exec -T postgres pg_dump -U dc doc_compare > backups/database.sql
tar --exclude='./pg' -czf backups/files.tar.gz -C data .
# 完成后启动；若此前进入维护状态，还需 ready / serve
docker compose up -d llm-ocr-compare
```

示例文件名每次使用前应替换为独立的备份批次，避免覆盖。`.env` 和授权字体另行安全备份。
灾难恢复应在应用停止的干净恢复环境中导入数据库、恢复匹配的文件，再启动并验证；先完成恢复演练，不在运行中的业务库上直接覆盖。

## 6. Nginx 和字体

宿主机 Nginx 的现有站点 `server` 块可使用以下配置；若 Nginx 也在容器内，应改为它能够访问的应用地址：

```nginx
# 按单次请求所有文件的总大小设置，并留出表单开销
client_max_body_size 120m;

location / {
    proxy_pass http://127.0.0.1:3012;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

SSE 进度需要关闭代理缓冲。域名与证书由部署方配置，修改后先 `nginx -t` 再重新加载。应用和数据库宿主端口限制在可信网段内。

镜像内置开源中文字体及常用 Word 字体回退。授权字体放入 `DC_FONTS_DIR`，仅在运行时只读挂载。检查字体命中：

```bash
docker compose exec llm-ocr-compare fc-match "等线"
docker compose exec llm-ocr-compare fc-match Arial
```

字体调整后重建应用容器并重新提交文档以生成新产物；既有报告不会自动重新渲染。

## 7. 故障检查

```bash
docker compose ps -a
docker compose logs --tail=100 llm-ocr-compare
docker compose logs --tail=100 postgres
docker compose exec llm-ocr-compare python -m document_comparison.release status
docker compose exec llm-ocr-compare alembic current
```

- 旧镜像提示“更换版本前必须先排空并 stop”：拉取包含修复的镜像或从当前源码重新构建。
- 应用运行但提交返回 503：检查维护状态；确认维护完成后 `release ready`，再 `release serve`。
- 数据库迁移失败：先检查迁移日志和版本兼容性，不通过删表或删除迁移文件强行启动。
- `release_activities` 存在未知操作：先暂停接单，确认 `status` 中对应 owner 的进程及后台写入已停止，核对任务、文件、回调和审计，再清除单个操作屏障：

```bash
bash scripts/release.sh ctl llm-ocr-compare resolve-activity \
  --activity-id ACTIVITY_ID --confirmed-stopped-owner OWNER_ID
```

该命令不修改任务或回调状态，也不能替代对账。历史 `0014_shadow_comparisons` 迁移及表结构仅用于数据库兼容，应用不再读写；无需回退或手工删除。
