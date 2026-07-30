# 文档比对系统（llm_orc_compare）

基于多模态 LLM API 的合同篡改检测系统。以原始 Word 合同为基准，比对回收的 PDF（文本 PDF 或扫描件），识别金额、日期、违约责任、管辖法院等条款是否发生变化，并生成可追溯的差异报告。

## 主要能力

- 解析 Word 正文和表格，并保留条款结构。
- 优先读取 PDF 原生文本；扫描页自动调用多模态 OCR。
- 支持 OpenAI 兼容的多模态模型，也可切换到 PaddleOCR-VL Chat Completions 服务；标准合同比对会用 `OCR:` 识别内容，并在缺少坐标时用 `Spotting:` 补充扫描 PDF 高亮位置。
- 默认通过保守编号/字段锚点、父章节上下文和分段单调 DP 对齐条款，原生支持
  `1↔1`、`1↔2`、`2↔1`；可选纯文本 LLM 可直接对 DOCX 段落与 PDF OCR block
  的字符区间联合分段和对齐。联合规划最多调用一次、最长等待 60 秒；非法、
  超时或不完整计划自动回退确定性 Clause 对齐，不再发起第二轮 LLM。
- 对金额、日期、主体、账号、责任等高风险要素进行 canonical 强校验，并可启用纯文本 LLM 辅助说明。
- **合同比对默认仅列举差异**(字符级 / 表格单元格级),不做风险判别;在提交选项里传 `enable_risk_assessment=true` 可恢复完整风险分级 + 高风险要素校验 + LLM 辅助说明。
- **LLM 直接比对**:`options.enable_llm_direct_diff=true`(或外部 API 默认配置 `external_enable_llm_direct_diff`)开启后,跳过条款切分/对齐/裁决,把 Word 与 PDF 各自解析成纯文本后直接交给 LLM 比对差异并标注;结果适配为标准报告,不做风险分级、不生成 PDF 高亮框(无坐标信息)。
- 提供标准条款比对和纯文本快速比对两种模式。
- **金额统计**:一次可上传多个对帐单 PDF(扫描件),OCR 识别表格后用确定性代码抽取并累加金额,聚合输出所有文件的总金额;自动核对「合计/小计」声明值;列定位失败时由多模态 LLM 仅指认金额列(不做算术)。
- 通过 SSE 实时展示任务进度、各阶段耗时，并支持任务完成 Webhook。
- 输出 JSON 报告、带差异标注的 PDF，以及带整段高亮的 Word 报告。
- 报告页可在线查看带坐标的 PDF 差异标注，并可下载高亮 Word 报告。
- **比对记录持久化**:每次任务(合同比对 / 无标注比对 / 对帐单统计)的元数据、里程碑事件与完整报告 JSON 都写入 Postgres;进程重启后报告仍可打开,并提供独立的「比对记录」页查询历史。

## 快速开始

推荐使用 Docker Compose，前端和后端会运行在同一个容器中。

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health
```

`docker-compose.yml` 会同时启动 `postgres` 服务并自动迁移数据库;应用容器依赖 PG 健康检查通过后才会启动。Postgres 数据持久化到宿主 `./data/pg/`。

默认访问地址为 <http://localhost:8000>。当前 `docker-compose.yml` 使用 `${DC_PORT:-3012}` 作为宿主机端口；复制 `.env.example` 后，端口为其中配置的 `DC_PORT=8000`。

首次使用时：

1. 打开页面右上角的“设置”。
2. 配置 OCR 服务的 API Base、API Key 和模型名。
3. 按需配置语义向量服务，以及条款对齐/差异说明共用的纯文本 LLM 服务。
4. 返回提交页，上传原始 `.docx` 和待核验 `.pdf`。
5. 等待任务完成后查看差异，并下载高亮 Word、PDF 或 JSON 报告。

配置通过 UI 写入同栈 Postgres(`llm_config` 表)，上传文件与日志保存在 `./data/`，**任务记录、完整报告 JSONB 与 LLM/OCR 模型配置只写入同栈 Postgres**(数据在 `./data/pg/`),容器重启后不会丢失。不要提交包含真实 API Key 的旧 `llm_config.json` 备份文件。

> **数据库迁移**:应用启动时会自动执行 `alembic upgrade head`(幂等),`docker compose up` 即用、无需手动进容器跑迁移。如需关闭自动迁移(交由 CI/运维手动控制),设置 `DC_DB_AUTO_MIGRATE=0`。

更完整的容器运维说明见 [Docker 部署指南](docs/deploy.md)。

## 并发与多 worker

默认单进程(`DC_UVICORN_WORKERS=1`)、并发任务上限 4(`DC_MAX_CONCURRENT_TASKS=4`)。

- **多 worker**:设 `DC_UVICORN_WORKERS=N` 启用 gunicorn 多 worker。任务提交 / 执行 / SSE / 查询可落在不同 worker,系统已通过 Postgres 共享任务状态(无 Redis 依赖)。
- **全局并发上限**:由 `task_records.status` 计数强制,跨 worker 一致;超限返回 `429`。执行端另有进程内信号量作第二道保险,获取槽位最长等待 `DC_TASK_ACQUIRE_TIMEOUT`(默认 30s),超时则任务置 `failed`,防止同步模式请求在槽位满时永久 hang。
- **SSE 进度**:多 worker 下读 PG 里程碑事件,粒度为里程碑级(start / `*_done` / done,约 6-10 个节点/任务),非逐页细粒度。
- **DB 连接数**:每个 worker 独立持有连接池(`DC_DB_POOL_SIZE` + `DC_DB_MAX_OVERFLOW`,默认 5+10=15)。N worker 下连接数上限 = N × 15,需确认 PG `max_connections`(默认 100)够用。

```bash
# 启用 2 worker、8 并发:
DC_UVICORN_WORKERS=2 DC_MAX_CONCURRENT_TASKS=8 docker compose up -d --build
```

> 回退:`DC_UVICORN_WORKERS=1` 即完全恢复单进程行为。

## 本地开发

### 后端

环境要求：Python 3.9+（容器使用 Python 3.12）。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
python -m document_comparison.api.app
```

后端默认监听 <http://localhost:8000>，交互式 API 文档位于 <http://localhost:8000/docs>。

### 前端

环境要求：Node.js 20+。

```bash
cd web
npm ci
npm run dev
```

开发服务器默认位于 <http://localhost:5173>，并将 `/api/*` 和 `/health` 代理到 `http://localhost:8000`。前端脚本与目录说明见 [web/README.md](web/README.md)。

## 常用命令

```bash
# 后端测试
pytest

# 前端类型检查与生产构建
cd web
npm run type-check
npm run build

# 查看容器日志
docker compose logs -f

# 查看持久化应用日志
tail -f data/logs/app.log
```

## 工作流程

1. **Word 解析**：使用 `python-docx` 提取正文和结构化表格。
2. **可信 PDF 读取**：文本 PDF 使用 PyMuPDF 原生解析；扫描页降级到多模态 LLM 或 PaddleOCR-VL。
3. **联合分段与对齐**：默认使用编号/字段锚点和语义相似度；可选 LLM 直接输出原始 block 字符区间分组，不依赖预先固定的 Clause 边界。
4. **差异检测**：先统一全半角、引号字形、中文排版空格和 canonical 事实，再由字符级及表格单元格 diff 决定是否变化；语义相似度仅用于条款对齐。
5. **差异说明（可选）**：将已确认修改交给纯文本 LLM 补充严重度建议，不能撤销变化。
6. **质量门禁与报告**：英文分词空格、OCR 易丢标点和低质量页面只标记为待复核；数字内部标点、负号、百分号及条款编号仍按内容变化处理。

## 配置说明

LLM/OCR 配置统一在 UI 设置页维护，并持久化到 Postgres(`llm_config` 表)：

| 配置组 | 用途 | 可选后端 |
| --- | --- | --- |
| OCR | 扫描页文字与版面识别 | `llm`；`paddleocr` 可切换 vLLM 兼容调用/官方 SDK/自建 PaddleX serving |
| Embedding | 无编号条款的语义对齐 | `qwen`、`bge`、`mock` |
| Judge | 对疑似修改条款做语义复核 | OpenAI 兼容的纯文本模型 |

PaddleOCR 的“官方 API / SDK”模式使用 AI Studio Access Token。填写官方示例提供的
完整 `/layout-parsing` 地址时，使用与网页端一致的同步版面解析 API；API 地址留空时
使用 `PaddleOCRClient` 异步任务服务，模型留空时默认 `PaddleOCR-VL-1.6`。切回
`vllm` 即继续使用原有 OpenAI 兼容 `/chat/completions` 路径；两套
Base/凭据/模型配置分开保存，切换不会覆盖另一套。
异步 SDK 文档解析任务的单次 HTTP 请求沿用 PaddleOCR 请求超时，总轮询时间
不少于 900 秒；如服务排队时间更长，可在设置页将请求超时提高到 3600 秒。

PaddleOCR 的“自建 PaddleX serving”模式(`paddlex_serving`)调用 `paddlex --serve`
部署的服务(如 `http://192.168.100.102:8080`)，整本 PDF 一次提交。端点路径默认
`/ocr`(通用 OCR 产线，返回文本行 + 检测框)，改成 `/layout-parsing` 则走
PP-StructureV3 版面解析产线(返回段落/表格/标题结构)。鉴权可选——自建 serving
默认无鉴权，仅服务端开启鉴权时填写 API Key；该模式不消耗 AI Studio 配额。三套
配置(vllm / official_sdk / paddlex_serving)分开保存，切换不会覆盖另一套。

“外部 API 配置”页集中维护合同比对与金额统计共用的 API Key、服务公开地址、单文件
上传上限、高亮图片 DPI 和 OCR 默认引擎；保存后立即生效，Key 只以脱敏值回显。
“合同比对 API”页仅维护比对流程选项并可上传 DOCX/PDF 运行完整管线测试；“金额统计
API”页提供多 PDF 的统计管线测试。两类测试都不发送真实回调。

服务端基础配置仍通过环境变量提供：

日志同时写入控制台和 `${DC_STORAGE_DIR}/logs/app.log`。每条日志都带 `task` 和
`req` 关联标识，可按任务或外部接口请求串联排障；模型请求/响应在文件日志中仅记录
模型、状态、耗时、长度和 SHA-256 摘要，不记录合同原文。需要查看受控调用明细时，使用
任务的模型/OCR记录接口。标准比对、无标注比对和金额统计都会额外保存最终被流水线采用的
OCR 解析结果，包括页码、块类型、坐标、字符数、内容 SHA-256 和受限文本预览；单块预览
最多 300 字、单条记录最多 64KB，结构化表格的 headers/rows 不重复展开，只记录行列维度。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DC_HOST` | `0.0.0.0` | 后端监听地址 |
| `DC_PORT` | `8000` | 后端监听端口；Compose 中也用作宿主机映射端口 |
| `DC_STORAGE_DIR` | `./.dc_data` | 上传文件、配置和日志目录(报告 JSON 不再写文件,只在 Postgres) |
| `DC_STATIC_DIR` | 空 | 前端静态文件目录；容器内已设为 `/app/static` |
| `DC_LOG_LEVEL` | `INFO` | 日志级别，可设为 `DEBUG` |
| `DATABASE_URL` | 空(必填) | Postgres 连接串;未配置时启动失败。docker-compose 自动注入 |
| `POSTGRES_PASSWORD` | `dcpass` | docker-compose 内置 PG 服务的密码(对应 `dc` 用户) |
| `DC_DB_AUTO_MIGRATE` | `1` | 启动时自动跑 `alembic upgrade head`(默认开);设为 `0` 改由 CI/运维手动控制 |
| `DC_DB_AUTO_CREATE` | 空 | 设为 `1` 时跳过 alembic 直接 `CREATE TABLE IF NOT EXISTS`(仅测试用) |
| `DC_EXTERNAL_API_KEY` | 空 | 外部 API Key(**可选**);留空=不鉴权,配置后 `/api/v1/external/*` 按 `X-API-Key` 校验。管理端设置可持久化覆盖 |
| `DC_EXTERNAL_PUBLIC_BASE_URL` | 空 | 服务公开地址的首次启动/灾备默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_MAX_UPLOAD_MB` | `50` | 外部接口单文件上限默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_IMAGE_DPI` | `144` | 外部高亮 PNG DPI 默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_OCR_BACKEND` | `paddleocr` | 外部正式调用与页面测试共用的 OCR 引擎默认值 |
| `DC_EXTERNAL_ENABLE_LLM_ALIGNMENT` | `0` | 是否默认启用 LLM 原始块联合分段对齐；失败自动回退 Clause 对齐 |
| `DC_EXTERNAL_ENABLE_LLM_JUDGE` | `0` | 是否默认启用 LLM 辅助说明；仅在风险评估开启时生效 |
| `DC_EXTERNAL_ENABLE_RISK_ASSESSMENT` | `0` | 是否为外部 API 开启风险分级；默认仍仅判定内容变化 |
| `DC_DOWNLOAD_TIMEOUT_SECONDS` | `60` | 外部接口以 URL(`source_url`/`target_url`)提交时,后端下载该 URL 的总超时(秒) |
| `DC_DOWNLOAD_MAX_REDIRECTS` | `5` | URL 下载的最大重定向跳数 |

## 目录结构

```text
.
├── docs/
│   ├── deploy.md                 # Docker 部署与运维
│   └── 技术方案.md               # 完整技术设计
├── alembic/                      # SQLAlchemy 迁移脚本(初始建表)
├── src/document_comparison/
│   ├── api/                      # FastAPI 接口
│   ├── db/                       # SQLAlchemy ORM、引擎、任务记录/事件持久化
│   ├── parsing/                  # Word / PDF 解析
│   ├── ocr/                      # LLM / PaddleOCR 引擎及质量检查
│   ├── structure/                # 条款切分与归一化
│   ├── align/                    # 条款对齐
│   ├── compare/                  # diff、风险要素、裁决与 LLM 辅助说明
│   ├── embed/                    # qwen / bge / mock 向量引擎
│   ├── report/                   # JSON、PDF、DOCX 报告
│   ├── pipeline.py               # 标准条款比对流水线
│   └── raw_pipeline.py           # 纯文本快速比对流水线
├── web/                          # Vue 3 + Vite + Pinia 前端
├── tests/                        # pytest 测试
├── scripts/                      # 流水线性能基准脚本
├── Dockerfile                    # 前后端多阶段构建
└── docker-compose.yml            # 应用 + Postgres 部署
```

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/v1/compare` | 提交标准条款比对(默认仅列举差异;`options.enable_risk_assessment=true` 开启风险判别) |
| `GET` | `/api/v1/compare/{task_id}` | 查询任务状态和结果 |
| `GET` | `/api/v1/compare/{task_id}/events` | 订阅 SSE 进度 |
| `GET` | `/api/v1/compare/{task_id}/report?format=json\|pdf\|docx` | 下载报告 |
| `POST` | `/api/v1/external/contractCompare` | 外部系统提交标准合同比对(`X-API-Key`);默认异步,`sync=true` 同步返回结果 |
| `GET` | `/api/v1/external/contractCompare/{task_id}` | 外部系统查询结果文本和全页高亮图片清单 |
| `GET` | `/api/v1/external/contractCompare/{task_id}/images/{page_number}` | 下载指定页高亮 PNG(`X-API-Key`) |
| `POST` | `/api/v1/raw-compare` | 提交纯文本快速比对 |
| `POST` | `/api/v1/statement` | 提交金额统计(支持多文件,`target` 字段同键多值) |
| `GET` | `/api/v1/statement/{task_id}` | 查询统计任务状态和结果 |
| `GET` | `/api/v1/statement/{task_id}/events` | 订阅 SSE 进度 |
| `GET` | `/api/v1/statement/{task_id}/report` | 获取统计报告(含总合计、按列汇总、逐行明细) |
| `POST` | `/api/v1/external/amountStat` | 外部系统提交金额统计(`X-API-Key`,多 PDF,支持 `target`/`target_urls` 混合);默认异步,`sync=true` 同步返回结果。详见 [金额统计对外 API 文档](docs/external-amount-api.md) |
| `GET` | `/api/v1/external/amountStat/{task_id}` | 外部系统查询金额统计结果(扁平汇总字段 + 完整明细 `report`) |
| `GET` | `/api/v1/config/llm` | 获取当前模型配置（Key 脱敏） |
| `PUT` | `/api/v1/config/llm` | 更新并持久化模型配置 |
| `GET` | `/api/v1/tasks?kind=&status=&limit=&offset=` | 查询任务历史(支持按类型/状态筛选 + 分页) |
| `GET` | `/api/v1/tasks/{task_id}/events` | 查询单任务的里程碑事件时间线 |
| `GET` | `/api/v1/tasks/{task_id}/llm-calls` | 查询单任务的模型调用与最终 OCR 解析结果明细(OCR/judge/llm-diff/ocr-result 等) |
| `GET` | `/api/v1/tasks/{task_id}/external-calls` | 查询单任务的外部接口入站调用审计记录 |
| `GET` | `/api/v1/external-calls?endpoint=&status_code=&document_no=&q=&limit=&offset=` | 全局外部接口调用审计列表(含 401/422 等无 task_id 的失败调用) |

### 外部合同比对调用示例

接口 `POST /api/v1/external/contractCompare`,请求体 `multipart/form-data`,公共表单参数:

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `source` | 与 `source_url` 二选一 | 原始合同 `.docx`(文件) |
| `source_url` | 与 `source` 二选一 | 原始合同 URL(`http`/`https` `.docx`);服务端下载后比对,大小限制同 `source` |
| `target` | 与 `target_url` 二选一 | 回收件 `.pdf`(文件) |
| `target_url` | 与 `target` 二选一 | 回收件 URL(`http`/`https` `.pdf`);服务端下载后比对,大小限制同 `target` |
| `document_no` | 是 | 外部单据号(≤255 字符) |
| `callback_url` | 异步必填,同步可选 | HTTP/HTTPS 完成回调地址 |
| `sync` | 否 | `true` 同步模式;缺省=`false` 异步模式 |

所有请求**默认**需带 `X-API-Key` 头;若服务端未配置 `DC_EXTERNAL_API_KEY`(管理端「LLM 配置 → 外部 API」留空),则跳过鉴权直接放行,适用于内网/可信环境。

#### 异步模式(默认,`sync` 缺省或 `false`)

提交即返回 `task_id`,比对在后台进行;`callback_url` 必填,结果经**回调**或**查询端点**获取。

```bash
curl -X POST 'https://compare.example.com/api/v1/external/contractCompare' \
  -H 'X-API-Key: <YOUR_API_KEY>' \
  -F 'source=@./original-contract.docx' \
  -F 'target=@./returned-contract.pdf' \
  -F 'document_no=DOC-2026-0001' \
  -F 'callback_url=https://business.example.com/callbacks/contract-compare'
```

> 每个角色(`source`/`target`)也可改用链接提交:把 `-F 'source=@./...'` 换成 `-F 'source_url=https://.../original-contract.docx'`,`target` 同理换 `-F 'target_url=...'`。文件与链接二选一,服务端下载后比对,后缀(`.docx`/`.pdf`)与大小限制不变。

提交响应(HTTP 202,仅 `task_id` + 状态):

```json
{
  "task_id": "a1b2c3d4e5f6",
  "document_no": "DOC-2026-0001",
  "status": "pending"
}
```

比对完成后用 `task_id` 主动查询(请求头需带 `X-API-Key`):

```bash
curl -H 'X-API-Key: <YOUR_API_KEY>' \
  'https://compare.example.com/api/v1/external/contractCompare/<TASK_ID>'
```

#### 同步模式(`sync=true`)

HTTP 连接保持至比对完成,完整结果**直接在提交响应体内**返回;此模式下 `callback_url` 非必填(传了也会触发回调,不传则不触发)。

```bash
curl -X POST 'https://compare.example.com/api/v1/external/contractCompare' \
  -H 'X-API-Key: <YOUR_API_KEY>' \
  -F 'source=@./original-contract.docx' \
  -F 'target=@./returned-contract.pdf' \
  -F 'document_no=DOC-2026-0001' \
  -F 'sync=true'
```

同步提交响应 / 异步查询响应(HTTP 200,顶层扁平结构;`result_text` 含逐条差异明细,识别状态与高亮定位并入其文案,不再单列字段):

```jsonc
{
  "task_id": "a1b2c3d4e5f6",
  "document_no": "DOC-2026-0001",
  "status": "done",
  "stage": "done",
  "progress": 1.0,
  "error": null,
  "change_status": "changed",            // clean / changed / needs_review
  "result_text": "单据号：DOC-2026-0001\n结论：发现确认内容变化\n识别状态：可靠\n高亮定位：完整\n差异数量：3\n[1] ...",
  "highlight_images": [
    "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6/images/1",
    "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6/images/2"
  ],
  "result_url": "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6"
}
// 任务失败时 status="failed"、error 为失败原因,其余结果字段缺省。
```

#### 完成回调(异步模式 `callback_url` 非空时触发)

比对结束(成功或失败)后,服务端向 `callback_url` 发起 `POST`,请求体为 JSON,请求头:

| 头 | 始终携带 | 说明 |
| --- | --- | --- |
| `Content-Type` | 是 | `application/json` |
| `X-Event-Id` | 是 | 事件唯一 ID,与 body 中 `event_id` 一致,便于幂等去重 |

比对完成回调 body:

```jsonc
{
  "event_id": "9f2c1b7a-1234-5678-9abc-def012345678",
  "task_id": "a1b2c3d4e5f6",
  "status": "done",
  "event_type": "contract.compare.completed",
  "document_no": "DOC-2026-0001",
  "change_status": "changed",            // clean / changed / needs_review
  "result_text": "单据号：DOC-2026-0001\n结论：发现确认内容变化\n识别状态：可靠\n高亮定位：完整\n差异数量：3\n[1] ...",
  "highlight_images": [
    "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6/images/1",
    "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6/images/2"
  ],
  "result_url": "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6"
}
```

比对失败回调 body(`status="failed"`,仅含失败原因):

```jsonc
{
  "event_id": "9f2c1b7a-1234-5678-9abc-def012345678",
  "task_id": "a1b2c3d4e5f6",
  "status": "failed",
  "event_type": "contract.compare.failed",
  "document_no": "DOC-2026-0001",
  "error": "OCR 解析失败"
}
```

> 投递失败按指数退避(1/4/16 秒)最多重试 3 次;调用方应按 `X-Event-Id`/`event_id` 幂等处理。

#### 下载高亮 PNG

```bash
# 响应体为二进制 PNG:
#   Content-Type: image/png
#   Content-Disposition: inline; filename="<TASK_ID>-page-0001.png"
curl -H 'X-API-Key: <YOUR_API_KEY>' \
  -o page-0001.png \
  'https://compare.example.com/api/v1/external/contractCompare/<TASK_ID>/images/1'
```

“合同比对 API”页也提供相同示例和一键复制功能；页面会自动使用已保存的服务公开地址，
但不会显示或写入真实 API Key。

#### 外部接口调用审计

每次对 `/api/v1/external/*` 的入站请求（提交、查询、图片下载，**含 401 鉴权失败、
413/422 校验失败**）都会落库一条审计记录到 `external_api_calls` 表，字段包括：

- `endpoint`（`contractCompare.submit` / `.result` / `.image` / `amountStat.submit` / `amountStat.result`）、`method`、`status_code`、`elapsed_ms`
- `client_ip`（取 `X-Forwarded-For[0]` / `X-Real-IP` / `client.host`）
- `api_key_sha256`（X-API-Key 的 sha256 指纹，**不存明文 Key**）
- `document_no`、`task_id`（提交成功时关联；401/422 等任务创建前失败为 `null`，且**不加外键**——任务删除不会清除审计记录）
- `error`（按状态码映射固定摘要，如 `invalid external API key`）、`request_id`、`content_length`、`created_at`

审计通过 HTTP 中间件统一写入，**异步落库、绝不阻塞响应**；写库失败只记日志。
每个响应都会带 `X-Request-Id` 响应头（与响应体 `request_id` 一致），便于调用方与服务端联查。
查询入口：按任务 `GET /api/v1/tasks/{task_id}/external-calls`（前端「比对记录 → 外部调用」tab），
或全局 `GET /api/v1/external-calls`（支持按端点/状态码/单据号筛选）。

### 外部金额统计调用示例

接口 `POST /api/v1/external/amountStat`，请求体 `multipart/form-data`，与合同比对共用 `X-API-Key` 鉴权、审计、回调机制，差异在于：输入为**多个对帐单 PDF**、产出为**金额汇总**（无高亮图片）、回调 `event_type` 为 `statement.summary.*`。完整字段字典与错误码见 [金额统计对外 API 文档](docs/external-amount-api.md)。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `target` | 与 `target_urls` 至少一个 | 对帐单/发票 `.pdf`（文件，可重复多次上传多个） |
| `target_urls` | 与 `target` 至少一个 | 对帐单 URL（`http`/`https` `.pdf`，可重复多次）；可与 `target` 混合提交，服务端下载后统计 |
| `document_no` | 是 | 外部单据号（≤255 字符） |
| `callback_url` | 异步必填，同步可选 | HTTP/HTTPS 完成回调地址 |
| `sync` | 否 | `true` 同步模式；缺省=`false` 异步模式 |

#### 异步模式（默认，`sync` 缺省或 `false`）

```bash
curl -X POST 'https://compare.example.com/api/v1/external/amountStat' \
  -H 'X-API-Key: <YOUR_API_KEY>' \
  -F 'target=@./statement-1.pdf' \
  -F 'target=@./statement-2.pdf' \
  -F 'document_no=STMT-2026-0001' \
  -F 'callback_url=https://business.example.com/callbacks/amount-stat'
```

> 也可用 URL 提交：把 `-F 'target=@./...'` 换成 `-F 'target_urls=https://.../statement-1.pdf'`，多个 URL 重复多次。文件与 URL 可混合，后缀（`.pdf`）与大小限制不变。

提交响应（HTTP 202）：

```json
{
  "task_id": "a1b2c3d4e5f6",
  "document_no": "STMT-2026-0001",
  "status": "pending"
}
```

统计完成后用 `task_id` 主动查询：

```bash
curl -H 'X-API-Key: <YOUR_API_KEY>' \
  'https://compare.example.com/api/v1/external/amountStat/<TASK_ID>'
```

#### 同步模式（`sync=true`）

HTTP 连接保持至统计完成，完整结果直接在提交响应体内返回；此模式下 `callback_url` 非必填。

```bash
curl -X POST 'https://compare.example.com/api/v1/external/amountStat' \
  -H 'X-API-Key: <YOUR_API_KEY>' \
  -F 'target=@./statement-1.pdf' \
  -F 'document_no=STMT-2026-0001' \
  -F 'sync=true'
```

同步提交响应 / 异步查询响应（HTTP 200，顶层扁平汇总字段 + 嵌套完整明细 `report`）：

```jsonc
{
  "task_id": "a1b2c3d4e5f6",
  "document_no": "STMT-2026-0001",
  "status": "done",
  "stage": "done",
  "progress": 1.0,
  "error": null,
  "grand_total": 100000.0,                 // 所有文件、所有金额列之和(核心输出)
  "verdict": "clean",                      // clean / changed / needs_review
  "file_totals": [
    { "file_index": 0, "file_name": "statement1.pdf", "total_amount": 60000.0, "error": null },
    { "file_index": 1, "file_name": "statement2.pdf", "total_amount": 40000.0, "error": null }
  ],
  "total_files": 2,
  "total_tables": 2,
  "total_items": 5,
  "reasons": [],
  "result_url": "https://compare.example.com/api/v1/external/amountStat/a1b2c3d4e5f6"
}
// 任务失败时 status="failed"、error 为失败原因,其余结果字段缺省。
```

> **算术确定性**：`grand_total` 与各级合计始终由代码用 `Decimal` 求和；LLM 仅在正则启发式列定位失败时兜底指认金额列，抽出的每个金额必须能在 OCR 文本中逐字溯源，否则丢弃并标记 `needs_review`。详见 [金额统计方案](docs/金额统计方案.md)。

#### 完成回调（异步模式 `callback_url` 非空时触发）

统计结束（成功或失败）后，服务端向 `callback_url` 发起 `POST`，请求头与合同比对回调一致（`Content-Type: application/json`、`X-Event-Id`），投递失败按指数退避（1/4/16 秒）最多重试 3 次。

成功回调 body（字段与查询 `done` 响应一致，去掉 `event_id`/`task_id`/`status` 信封）：

```jsonc
{
  "event_id": "9f2c1b7a-1234-5678-9abc-def012345678",
  "task_id": "a1b2c3d4e5f6",
  "status": "done",
  "event_type": "statement.summary.completed",
  "document_no": "STMT-2026-0001",
  "grand_total": 100000.0,
  "verdict": "clean",
  "file_totals": [
    { "file_index": 0, "file_name": "statement1.pdf", "total_amount": 60000.0, "error": null },
    { "file_index": 1, "file_name": "statement2.pdf", "total_amount": 40000.0, "error": null }
  ],
  "total_files": 2,
  "total_tables": 2,
  "total_items": 5,
  "reasons": [],
  "result_url": "https://compare.example.com/api/v1/external/amountStat/a1b2c3d4e5f6"
}
```

失败回调 body：

```jsonc
{
  "event_id": "9f2c1b7a-1234-5678-9abc-def012345678",
  "task_id": "a1b2c3d4e5f6",
  "status": "failed",
  "event_type": "statement.summary.failed",
  "document_no": "STMT-2026-0001",
  "error": "OCR 解析失败"
}
```

#### 管线测试（免鉴权）

与合同比对页对称，金额统计也提供免鉴权的管线测试端点，支持重复 `target` 文件和/或重复 `target_urls`（HTTP/HTTPS PDF URL），供本机前端在不触发真实回调的前提下验证整条 OCR + 表格抽取 + 求和管线（强制 `document_no` 以 `API-TEST-` 开头，查询端点据此隔离真实任务）：

```bash
# 提交(无需 X-API-Key,可多选 PDF)
curl -X POST 'https://compare.example.com/api/v1/statement/api-test' \
  -F 'target=@./statement-1.pdf' \
  -F 'target=@./statement-2.pdf'

# 轮询查询结果(响应结构与对外 amountStat 查询一致)
curl 'https://compare.example.com/api/v1/statement/api-test/<TASK_ID>'
```

完整数据结构和设计取舍见 [技术方案](docs/技术方案.md)；金额统计的设计与边界见 [金额统计方案](docs/金额统计方案.md)。
