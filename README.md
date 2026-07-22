# 文档比对系统（llm_orc_compare）

基于多模态 LLM API 的合同篡改检测系统。以原始 Word 合同为基准，比对回收的 PDF（文本 PDF 或扫描件），识别金额、日期、违约责任、管辖法院等条款是否发生变化，并生成可追溯的差异报告。

## 主要能力

- 解析 Word 正文和表格，并保留条款结构。
- 优先读取 PDF 原生文本；扫描页自动调用多模态 OCR。
- 支持 OpenAI 兼容的多模态模型，也可切换到 PaddleOCR-VL Chat Completions 服务；标准合同比对会用 `OCR:` 识别内容，并在缺少坐标时用 `Spotting:` 补充扫描 PDF 高亮位置。
- 通过编号、字段和语义向量对齐条款，执行字符级及表格单元格级 diff。
- 对金额、日期、主体、账号、责任等高风险要素进行 canonical 强校验，并可启用纯文本 LLM 辅助说明。
- **合同比对默认仅列举差异**(字符级 / 表格单元格级),不做风险判别;在提交选项里传 `enable_risk_assessment=true` 可恢复完整风险分级 + 高风险要素校验 + LLM 辅助说明。
- 提供标准条款比对和纯文本快速比对两种模式。
- **对帐单金额统计**:一次可上传多个对帐单 PDF(扫描件),OCR 识别表格后用确定性代码抽取并累加金额,聚合输出所有文件的总金额;自动核对「合计/小计」声明值;列定位失败时由多模态 LLM 仅指认金额列(不做算术)。
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
3. 按需配置语义向量服务和 LLM 辅助说明服务。
4. 返回提交页，上传原始 `.docx` 和待核验 `.pdf`。
5. 等待任务完成后查看差异，并下载高亮 Word、PDF 或 JSON 报告。

配置通过 UI 写入同栈 Postgres(`llm_config` 表)，上传文件与日志保存在 `./data/`，**任务记录、完整报告 JSONB 与 LLM/OCR 模型配置只写入同栈 Postgres**(数据在 `./data/pg/`),容器重启后不会丢失。不要提交包含真实 API Key 的旧 `llm_config.json` 备份文件。

> **数据库迁移**:应用启动时会自动执行 `alembic upgrade head`(幂等),`docker compose up` 即用、无需手动进容器跑迁移。如需关闭自动迁移(交由 CI/运维手动控制),设置 `DC_DB_AUTO_MIGRATE=0`。

更完整的容器运维说明见 [Docker 部署指南](docs/deploy.md)。

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
3. **条款对齐**：使用编号锚点、字段锚点和语义相似度匹配两侧条款。
4. **差异检测**：先统一全半角、引号字形、中文排版空格和 canonical 事实，再由字符级及表格单元格 diff 决定是否变化；语义相似度仅用于条款对齐。
5. **差异说明（可选）**：将已确认修改交给纯文本 LLM 补充严重度建议，不能撤销变化。
6. **质量门禁与报告**：英文分词空格、OCR 易丢标点和低质量页面只标记为待复核；数字内部标点、负号、百分号及条款编号仍按内容变化处理。

## 配置说明

LLM/OCR 配置统一在 UI 设置页维护，并持久化到 Postgres(`llm_config` 表)：

| 配置组 | 用途 | 可选后端 |
| --- | --- | --- |
| OCR | 扫描页文字与版面识别 | `llm`；`paddleocr` 可切换 vLLM 兼容调用/官方 SDK |
| Embedding | 无编号条款的语义对齐 | `qwen`、`bge`、`mock` |
| Judge | 对疑似修改条款做语义复核 | OpenAI 兼容的纯文本模型 |

PaddleOCR 的“官方 API / SDK”模式使用 AI Studio Access Token。填写官方示例提供的
完整 `/layout-parsing` 地址时，使用与网页端一致的同步版面解析 API；API 地址留空时
使用 `PaddleOCRClient` 异步任务服务，模型留空时默认 `PaddleOCR-VL-1.6`。切回
`vllm` 即继续使用原有 OpenAI 兼容 `/chat/completions` 路径；两套
Base/凭据/模型配置分开保存，切换不会覆盖另一套。
异步 SDK 文档解析任务的单次 HTTP 请求沿用 PaddleOCR 请求超时，总轮询时间
不少于 900 秒；如服务排队时间更长，可在设置页将请求超时提高到 3600 秒。

“合同比对 API”页集中维护外部调用 API Key、服务公开地址、单文件上传上限、高亮
图片 DPI 以及 OCR/LLM 比对选项；保存后立即生效，Key 只以脱敏值回显。该页也可
上传 DOCX/PDF 运行完整管线测试，验证结果文本和逐页高亮图片，且不发送真实回调。

服务端基础配置仍通过环境变量提供：

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
| `DC_EXTERNAL_API_KEY` | 空 | 外部 API Key 的首次启动/灾备默认值；管理端设置可持久化覆盖 |
| `DC_EXTERNAL_PUBLIC_BASE_URL` | 空 | 服务公开地址的首次启动/灾备默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_MAX_UPLOAD_MB` | `50` | 外部接口单文件上限默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_IMAGE_DPI` | `144` | 外部高亮 PNG DPI 默认值；管理端设置可覆盖 |
| `DC_EXTERNAL_OCR_BACKEND` | `paddleocr` | 外部正式调用与页面测试共用的 OCR 引擎默认值 |
| `DC_EXTERNAL_ENABLE_LLM_JUDGE` | `0` | 是否默认启用 LLM 辅助说明；仅在风险评估开启时生效 |
| `DC_EXTERNAL_ENABLE_RISK_ASSESSMENT` | `0` | 是否为外部 API 开启风险分级；默认仍仅判定内容变化 |

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
| `GET` | `/api/v1/compare/{task_id}/docx-preview` | 获取 Word 全文及差异标记 |
| `POST` | `/api/v1/external/contractCompare` | 外部系统提交标准合同比对(`X-API-Key`);默认异步,`sync=true` 同步返回结果 |
| `GET` | `/api/v1/external/contractCompare/{task_id}` | 外部系统查询结果文本和全页高亮图片清单 |
| `GET` | `/api/v1/external/contractCompare/{task_id}/images/{page_number}` | 下载指定页高亮 PNG(`X-API-Key`) |
| `POST` | `/api/v1/raw-compare` | 提交纯文本快速比对 |
| `POST` | `/api/v1/statement` | 提交对帐单金额统计(支持多文件,`target` 字段同键多值) |
| `GET` | `/api/v1/statement/{task_id}` | 查询统计任务状态和结果 |
| `GET` | `/api/v1/statement/{task_id}/events` | 订阅 SSE 进度 |
| `GET` | `/api/v1/statement/{task_id}/report` | 获取统计报告(含总合计、按列汇总、逐行明细) |
| `GET` | `/api/v1/config/llm` | 获取当前模型配置（Key 脱敏） |
| `PUT` | `/api/v1/config/llm` | 更新并持久化模型配置 |
| `GET` | `/api/v1/tasks?kind=&status=&limit=&offset=` | 查询任务历史(支持按类型/状态筛选 + 分页) |
| `GET` | `/api/v1/tasks/{task_id}/events` | 查询单任务的里程碑事件时间线 |
| `GET` | `/api/v1/tasks/{task_id}/llm-calls` | 查询单任务的对话型 LLM 调用明细(OCR/judge/llm-diff 等) |
| `GET` | `/api/v1/tasks/{task_id}/external-calls` | 查询单任务的外部接口入站调用审计记录 |
| `GET` | `/api/v1/external-calls?endpoint=&status_code=&document_no=&q=&limit=&offset=` | 全局外部接口调用审计列表(含 401/422 等无 task_id 的失败调用) |

### 外部合同比对调用示例

```bash
# 提交任务(默认异步:返回 task_id,结果经回调或查询端点获取)
curl -X POST 'https://compare.example.com/api/v1/external/contractCompare' \
  -H 'X-API-Key: <YOUR_API_KEY>' \
  -F 'source=@./original-contract.docx' \
  -F 'target=@./returned-contract.pdf' \
  -F 'document_no=DOC-2026-0001' \
  -F 'callback_url=https://business.example.com/callbacks/contract-compare' \
  -F 'callback_secret=<YOUR_CALLBACK_SECRET>'   # 可选,留空则回调不带签名

# 同步模式(sync=true):HTTP 连接保持至比对完成,响应体内直接返回完整结果;
# 此模式下 callback_url 非必填(不传则不触发回调)。
curl -X POST 'https://compare.example.com/api/v1/external/contractCompare' \
  -H 'X-API-Key: <YOUR_API_KEY>' \
  -F 'source=@./original-contract.docx' \
  -F 'target=@./returned-contract.pdf' \
  -F 'document_no=DOC-2026-0001' \
  -F 'sync=true'

# 使用响应中的 task_id 查询完整结果(异步模式)
curl -H 'X-API-Key: <YOUR_API_KEY>' \
  'https://compare.example.com/api/v1/external/contractCompare/<TASK_ID>'

# 下载第 1 页高亮 PNG
curl -H 'X-API-Key: <YOUR_API_KEY>' \
  -o page-0001.png \
  'https://compare.example.com/api/v1/external/contractCompare/<TASK_ID>/images/1'
```

“合同比对 API”页也提供相同示例和一键复制功能；页面会自动使用已保存的服务公开地址，
但不会显示或写入真实 API Key、回调密钥。

#### 外部接口调用审计

每次对 `/api/v1/external/*` 的入站请求（提交、查询、图片下载，**含 401 鉴权失败、
413/422 校验失败**）都会落库一条审计记录到 `external_api_calls` 表，字段包括：

- `endpoint`（`contractCompare.submit` / `.result` / `.image`）、`method`、`status_code`、`elapsed_ms`
- `client_ip`（取 `X-Forwarded-For[0]` / `X-Real-IP` / `client.host`）
- `api_key_sha256`（X-API-Key 的 sha256 指纹，**不存明文 Key**）
- `document_no`、`task_id`（提交成功时关联；401/422 等任务创建前失败为 `null`，且**不加外键**——任务删除不会清除审计记录）
- `error`（按状态码映射固定摘要，如 `invalid external API key`）、`request_id`、`content_length`、`created_at`

审计通过 HTTP 中间件统一写入，**异步落库、绝不阻塞响应**；写库失败只记日志。
每个响应都会带 `X-Request-Id` 响应头（与响应体 `request_id` 一致），便于调用方与服务端联查。
查询入口：按任务 `GET /api/v1/tasks/{task_id}/external-calls`（前端「比对记录 → 外部调用」tab），
或全局 `GET /api/v1/external-calls`（支持按端点/状态码/单据号筛选）。

完整数据结构和设计取舍见 [技术方案](docs/技术方案.md)；对帐单金额统计的设计与边界见 [对帐单金额统计方案](docs/对帐单金额统计方案.md)。
