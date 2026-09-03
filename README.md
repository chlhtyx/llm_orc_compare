# 文档比对系统（llm_orc_compare）

基于多模态 LLM API 的合同篡改检测系统。以原始合同（Word `.docx` 或 PDF `.pdf`）为基准，比对回收的 PDF（文本 PDF 或扫描件），识别金额、日期、违约责任、管辖法院等条款是否发生变化，并生成可追溯的差异报告。原始合同为 PDF 时，复用原生文本层优先 + OCR 兜底解析通道（与回收件同路径）。

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
- **LLM 直接比对**:`options.enable_llm_direct_diff=true`(或外部 API 默认配置 `external_enable_llm_direct_diff`)开启后,跳过条款切分/对齐/裁决,把 Word 与 PDF 各自解析成纯文本后直接交给 LLM 比对差异；结果适配为标准报告,不做风险分级。原件/回收件存在可验证版面坐标时会生成对应高亮，缺少坐标则只输出文字差异。
- 提供标准条款比对和纯文本快速比对两种模式。
- **金额统计**:一次可上传多个对帐单 PDF(扫描件),OCR 识别表格后用确定性代码抽取并累加金额,聚合输出所有文件的总金额;自动核对「合计/小计」声明值;列定位失败时由多模态 LLM 仅指认金额列(不做算术)。
- 通过 SSE 实时展示任务进度、各阶段耗时，并支持任务完成 Webhook。
- 输出 JSON 报告、带差异标注的 PDF，以及带整段高亮的 Word 报告。
- 原件为 PDF 时直接标注；原件为 DOCX 时由容器内 LibreOffice 生成任务私有派生 PDF，再以其中可验证的文本块坐标标注。报告页、PDF 下载和外部 HTML 可同时展示原件侧旧值/删除内容与回收件侧新值/新增内容；映射不唯一或渲染失败时明确标为部分可用/不可用，不会伪造位置。
- 外部自包含 HTML 在两侧高亮图均可用时始终以原件/回收件左右分栏展示；滚动任一侧会按页面内容进度同步另一侧，窄屏按可用宽度缩小双栏内容。两份文件页数不一致(如 DOCX 派生渲染分页与回收件不同)时，HTML/PDF 报告都会注明双方页数，页码按各自文档独立展示。
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

### DOCX 渲染字体

镜像内置 Noto CJK、Liberation、Carlito 和 Caladea，并为等线、微软雅黑、宋体、Arial、Calibri、Cambria 等常见 Word 字体配置开源回退。若合同使用的字体有授权，请把 `.ttf`、`.ttc` 或 `.otf` 放入部署机的专用目录，并在 `.env` 设置 `DC_FONTS_DIR`（默认 `./fonts`）。该目录会以只读方式挂载到容器，字体不会进入镜像、Git 仓库或任务数据目录。

字体可减少换行和分页漂移，但不能承诺与 Microsoft Word 完全一致。部署后可运行 `docker compose exec llm-ocr-compare fc-match "等线"` 和 `docker compose exec llm-ocr-compare fc-match Arial` 确认实际命中字体；修改字体后请执行 `docker compose up -d --build` 再重新提交 DOCX 任务。

首次使用时：

1. 打开页面右上角的“设置”。
2. 配置 OCR 服务的 API Base、API Key 和模型名。
3. 按需配置语义向量服务，以及条款对齐/差异说明共用的纯文本 LLM 服务。
4. 返回提交页，上传原始合同（`.docx` 或 `.pdf`）和待核验 `.pdf`。
5. 等待任务完成后查看差异，并下载高亮 Word、PDF 或 JSON 报告。

配置通过 UI 写入同栈 Postgres(`llm_config` 表)，上传文件与日志保存在 `./data/`，**任务记录、完整报告 JSONB 与 LLM/OCR 模型配置只写入同栈 Postgres**(数据在 `./data/pg/`),容器重启后不会丢失。不要提交包含真实 API Key 的旧 `llm_config.json` 备份文件。

> **数据库迁移**:应用启动时会自动执行 `alembic upgrade head`(幂等),`docker compose up` 即用、无需手动进容器跑迁移。如需关闭自动迁移(交由 CI/运维手动控制),设置 `DC_DB_AUTO_MIGRATE=0`。

更完整的容器运维说明见 [Docker 部署指南](docs/deploy.md)。

## 漏洞扫描防护

应用默认拒绝路径穿越、敏感文件（如 `.env`、`.git`）和常见 CMS/运维端点探测，并对所有响应添加 `nosniff`、禁止页面嵌入、Referrer 与浏览器权限限制等基础安全头。被拒绝请求统一返回 `404`，日志只保留探测类别，不记录原始载荷或客户端地址。

可用 `DC_SCAN_PROTECTION_ENABLED=0` 或 `DC_SECURITY_HEADERS_ENABLED=0` 临时关闭对应应用层能力。公网部署仍应在反向代理/WAF 层配置 HTTPS、请求体大小限制、基于真实客户端 IP 的共享限速和告警；不要使用每个 worker 独立的内存限速器。

## 控制台访问口令

Web 控制台默认无鉴权,适合内网/可信环境。若部署端口可达范围超出可信网络,设置 `DC_CONSOLE_PASSWORD` 启用口令登录:所有控制台业务接口(任务提交/查询、报告与合同原文下载、模型配置读写、比对记录与调用明细等)都需要先在登录页输入口令换取会话 Cookie。

- 登录:`POST /api/v1/auth/login`;会话为无状态 HMAC 签名 Cookie(`HttpOnly` + `SameSite=Lax`),多 worker 部署下任意进程可独立校验,无需共享存储。
- 会话有效期 `DC_CONSOLE_SESSION_TTL_HOURS`(默认 12 小时);修改口令会使所有已发会话立即失效。
- 登录失败按来源 IP 限速(每 worker 独立计数,5 分钟窗口内 10 次失败后临时拒绝)。
- 豁免范围:外部接口 `/api/v1/external/*` 与 `/api/v1/compare/api-test` 不受影响(仍走 `X-API-Key`);`/health`、`/api/v1/version`、`/api/v1/auth/*` 与前端静态资源保持开放。
- 控制台内的「外部 API 配置」若尚未配置 `external_api_key`,设置控制台口令尤其重要——否则任何人都能通过无鉴权的 `PUT /api/v1/config/llm` 改写外部接口鉴权与模型配置。

```bash
# .env 中设置后 docker compose up -d 生效
DC_CONSOLE_PASSWORD=强口令
DC_CONSOLE_SESSION_TTL_HOURS=12   # 可选,默认 12
```

## 并发与多 worker

默认单进程(`DC_UVICORN_WORKERS=1`)、并发任务上限 4(`DC_MAX_CONCURRENT_TASKS=4`)。

- **多 worker**:设 `DC_UVICORN_WORKERS=N` 启用 gunicorn 多 worker。任务提交 / 执行 / SSE / 查询可落在不同 worker,系统已通过 Postgres 共享任务状态(无 Redis 依赖)。
- **执行并发与队列**:最多 `DC_MAX_CONCURRENT_TASKS` 个任务处于 `running`；外部接口满载时进入 PostgreSQL 持久化 `pending` 队列，容量由 `DC_MAX_QUEUED_TASKS`（默认 50）限制，队列满才返回 `429`。多 worker 通过事务锁原子领取，worker 异常后租约到期的任务会回到队列。
- **同步请求**:`sync=true` 优先等待排队并返回原有 `200 + 完整结果`；若在 `DC_SYNC_QUEUE_WAIT_SECONDS`（默认 300 秒）内仍未获得执行槽位，返回 `202 + task_id`，任务继续执行，可查询或等待回调。
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

1. **原始合同解析**：Word（`.docx`）用 `python-docx` 提取正文和结构化表格；PDF（`.pdf`）复用原生文本层优先 + OCR 兜底通道（与回收件同路径）。
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
| Judge | 对疑似修改条款做语义复核 | 纯文本模型,协议三选一(见下) |
| LLM 直接比对提示词 | 「LLM 直接比对」(无标注版管线 / `enable_llm_direct_diff` 分支)的系统提示词 | 复用 Judge 的纯文本模型；设置页可自定义，留空用内置默认规则 |

「llm 引擎」与「LLM 联合分段对齐与辅助说明服务」两组配置各有一个**接口协议**选项
(`llm_api_protocol` / `judge_api_protocol`,持久化到 Postgres,保存后立即生效):

| 协议 | 端点 | 适用 |
| --- | --- | --- |
| OpenAI 兼容(默认) | `{base}/chat/completions` | vLLM / SGLang / DashScope / SiliconFlow 等 |
| OpenAI Responses | `{base}/responses` | OpenAI 新版 Responses API 网关(vLLM 0.10+) |
| Anthropic Messages | `{base}/messages` | 官方 Claude API 或 Anthropic 兼容网关 |

注意事项:Anthropic 协议的 API Base 需含 `/v1`(如 `https://api.anthropic.com/v1`);
`max_tokens` 默认 8192(超出会在调用记录中留截断告警)。Responses / Anthropic 两条
通道不发送 `response_format` 与 vLLM 专属的 `chat_template_kwargs`(JSON 输出由提示词
约束 + 容错解析保证,思考链由 `<think>` 块剥离兜底)。向量引擎与 PaddleOCR 不受该
选项影响(Anthropic 无 embeddings API;PaddleOCR 固定走各自的三种调用方式)。

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

共享设置中的“红章遮挡恢复”默认关闭。开启后，系统仍先按当前 DPI 对原图做
普通 OCR；图像检测到红色印章时，仅将对应页面按“印章恢复 DPI”(默认 300)
重新渲染，生成红通道增强图并追加一次 OCR。印章区域外保留原图结果，区域内
采用二次结果；两次结果冲突、缺少坐标或二次识别失败时，该页通过现有质量门禁
标记为 `needs_review`，不会把原始合同内容自动填回回收件，也不会猜测被完全
遮挡的金额、日期、账号或税号。该逻辑同时覆盖通用 LLM、PaddleOCR vLLM、
官方 SDK、同步 `/layout-parsing` 与 PaddleX serving；无印章页面不增加模型调用。

“外部 API 配置”页集中维护合同比对与金额统计共用的 API Key、服务公开地址、单文件
上传上限、高亮图片 DPI 和 OCR 默认引擎；保存后立即生效，Key 只以脱敏值回显。
“合同比对 API”页仅维护比对流程选项并可上传 DOCX/PDF 运行完整管线测试；“金额统计
API”页提供多 PDF 的统计管线测试。两类测试都不发送真实回调。

服务端基础配置仍通过环境变量提供：

日志同时写入控制台和 `${DC_STORAGE_DIR}/logs/app.log`。每条日志都带【业务-步骤】
标注与 `task` 和 `req` 关联标识:任务执行期日志标注业务与所处流水线阶段(如
`【compare-ocr】`、`【statement-file-2】`,业务 = compare/raw/statement,阶段由进度
回调自动切换),任务级日志只标业务(`【compare】`),非任务日志(启动、API 请求)
显示 `【-】`;可按标注快速过滤某业务某步骤的日志,也可按任务号或外部请求串联排障。
模型请求/响应在文件日志中仅记录
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
| `DC_DOCX_RENDERER_PATH` | `soffice` | DOCX 原件派生 PDF 的 LibreOffice 可执行文件；容器内为 `/usr/bin/soffice` |
| `DC_DOCX_RENDER_TIMEOUT_SECONDS` | `90` | 单个 DOCX 渲染超时；超时只降级原件侧标注，不中断文字比对 |
| `DC_FONTS_DIR` | `./fonts` | Compose 只读挂载的授权字体目录；字体不参与 Docker 构建，供 LibreOffice/Fontconfig 运行时发现 |
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
| `GET` | `/api/v1/compare/{task_id}/report?format=json\|pdf\|html` | 下载报告；HTML 为自包含文件，差异行可跳转到高亮页；待人工核对时会列出业务可读的核对事项 |
| `GET` | `/api/v1/compare/{task_id}/report.pdf` | 下载自包含 PDF 比对报告（概要+差异明细+逐页高亮图，与外部 `result_url` 同一产物）；待人工核对时会列出业务可读的核对事项；盘上缺失时按需生成 |
| `GET` | `/api/v1/compare/{task_id}/report?format=pdf&side=source` | 下载原件侧标注；DOCX 使用 LibreOffice 派生 PDF，默认 `side=target` 为回收件 |
| `GET` | `/api/v1/compare/{task_id}/original-pdf` | 预览原件 PDF，或 DOCX 的 LibreOffice 派生 PDF（仅用于视觉标注） |
| `POST` | `/api/v1/external/contractCompare` | 外部系统提交标准合同比对(`X-API-Key`);默认异步,`sync=true` 同步返回结果 |
| `GET` | `/api/v1/external/contractCompare/{task_id}` | 外部系统查询结果文本和全页高亮图片清单 |
| `GET` | `/api/v1/external/contractCompare/{task_id}/images/{page_number}` | 下载指定页高亮 PNG(`X-API-Key`) |
| `GET` | `/api/v1/external/contractCompare/{task_id}/source-images/{page_number}` | 下载原件侧指定页高亮 PNG；DOCX 使用派生 PDF(`X-API-Key`) |
| `GET` | `/api/v1/external/contractCompare/{task_id}/report.html` | 下载自包含 HTML 比对报告(`X-API-Key`;`html_url` 指向此处,文件名 `【单据号】对比+时间.html`) |
| `GET` | `/api/v1/external/contractCompare/{task_id}/report.pdf` | 下载自包含 PDF 比对报告(`X-API-Key`;`result_url` 指向此处,概要+差异明细+逐页高亮图,文件名 `【单据号】对比+时间.pdf`) |
| `POST` | `/api/v1/raw-compare` | 提交纯文本快速比对 |
| `POST` | `/api/v1/statement` | 提交金额统计(支持多文件,`target` 字段同键多值) |
| `GET` | `/api/v1/statement/{task_id}` | 查询统计任务状态和结果 |
| `GET` | `/api/v1/statement/{task_id}/events` | 订阅 SSE 进度 |
| `GET` | `/api/v1/statement/{task_id}/report` | 获取统计报告(含总合计、按列汇总、逐行明细) |
| `POST` | `/api/v1/external/amountStat` | 外部系统提交金额统计(`X-API-Key`,多 PDF,支持 `target`/`target_urls` 混合);默认异步,`sync=true` 同步返回结果。详见 [金额统计对外 API 文档](docs/external-amount-api.md) |
| `GET` | `/api/v1/external/amountStat/{task_id}` | 外部系统查询金额统计结果(扁平汇总字段 + 完整明细 `report`) |
| `GET` | `/api/v1/config/llm` | 获取当前模型配置（Key 脱敏） |
| `PUT` | `/api/v1/config/llm` | 更新并持久化模型配置 |
| `GET` | `/api/v1/tasks?kind=&status=&document_type=&limit=&offset=` | 查询任务历史(支持按类型/状态/单据类型筛选 + 分页;`document_type` 为金额统计任务的发票(1)/对帐单(2)细分) |
| `GET` | `/api/v1/tasks/{task_id}/source/download` | 下载历史任务的原始上传文件(保留 `.docx`/`.pdf` 原格式；文件清理后返回 404) |
| `GET` | `/api/v1/tasks/{task_id}/events` | 查询单任务的里程碑事件时间线 |
| `GET` | `/api/v1/tasks/{task_id}/llm-calls` | 查询单任务的模型调用与最终 OCR 解析结果明细(OCR/judge/llm-diff/ocr-result 等) |
| `GET` | `/api/v1/tasks/{task_id}/external-calls` | 查询单任务的外部接口入站调用审计记录 |
| `GET` | `/api/v1/external-calls?endpoint=&status_code=&document_no=&q=&limit=&offset=` | 全局外部接口调用审计列表(含 401/422 等无 task_id 的失败调用) |
| `GET` | `/api/v1/stats/daily?days=14` / `?start=&end=` | 每日调用统计(看板):按天(北京时间)聚合任务数、模型调用数、外部接口调用数,含成功/失败与类型细分,缺失日期补零;`start`/`end`(YYYY-MM-DD)任一提供即自由选区间(缺省侧由今天/`days` 补齐,跨度上限 366 天) |

### 外部合同比对调用示例

接口 `POST /api/v1/external/contractCompare`,请求体 `multipart/form-data`,公共表单参数:

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `source` | 与 `source_url` 二选一 | 原始合同 `.docx`/`.pdf`(文件) |
| `source_url` | 与 `source` 二选一 | 原始合同 URL(`http`/`https` `.docx`/`.pdf`);服务端下载后比对,大小限制同 `source` |
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
  "result_url": "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6/report.pdf",
  "html_url": "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6/report.html"
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
  "result_url": "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6/report.pdf",
  "html_url": "https://compare.example.com/api/v1/external/contractCompare/a1b2c3d4e5f6/report.html"
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
- `request_params`（请求参数快照：提交类记录表单字段与 `callback_url`/`sync` 等，**文件只记文件名**；图片类记录 `page_number`。**失败拦截同样记录**——401 鉴权失败、422 参数校验失败发生在端点执行前，由审计中间件预读表单字段兜底。敏感 key 脱敏、超长字符串截断；chunked 无 Content-Length 或 body 超过上传上限的请求不预读，为 `null`）
- `error`（按状态码映射固定摘要，如 `invalid external API key`）、`request_id`、`content_length`、`created_at`

审计通过 HTTP 中间件统一写入，**异步落库、绝不阻塞响应**；写库失败只记日志。
每个响应都会带 `X-Request-Id` 响应头（与响应体 `request_id` 一致），便于调用方与服务端联查。
查询入口：按任务 `GET /api/v1/tasks/{task_id}/external-calls`（前端「比对记录 → 外部调用」tab），
或全局 `GET /api/v1/external-calls`（支持按端点/状态码/单据号筛选）。
每日调用量的汇总视图见控制台「看板」页（`/dashboard`，数据来自 `GET /api/v1/stats/daily`，
按天聚合任务数、模型调用数与外部接口调用数）。

### 外部金额统计调用示例

接口 `POST /api/v1/external/amountStat`，请求体 `multipart/form-data`，与合同比对共用 `X-API-Key` 鉴权、审计、回调机制，差异在于：输入为**多个对帐单 PDF**、产出为**金额汇总**（无高亮图片）、回调 `event_type` 为 `statement.summary.*`。完整字段字典与错误码见 [金额统计对外 API 文档](docs/external-amount-api.md)。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `target` | 与 `target_urls` 至少一个 | 对帐单/发票 `.pdf`（文件，可重复多次上传多个） |
| `target_urls` | 与 `target` 至少一个 | 对帐单 URL 数组（`http`/`https` `.pdf`）；multipart 推荐传 JSON 字符串数组，也兼容同名字段重复，可与 `target` 混合提交 |
| `document_no` | 是 | 外部单据号（≤255 字符） |
| `document_type` | 否 | **单据类型**（字符串枚举）：`1`=发票（默认）/ `2`=对帐单；用于区分统计对象，入库并在控制台「对比记录」中标识，支持后续按类型查询；兼容中文别名（发票/对帐单），非法值 → `400` |
| `callback_url` | 异步必填，同步可选 | HTTP/HTTPS 完成回调地址 |
| `sync` | 否 | `true` 同步模式；缺省=`false` 异步模式 |

#### 异步模式（默认，`sync` 缺省或 `false`）

```bash
curl -X POST 'https://compare.example.com/api/v1/external/amountStat' \
  -H 'X-API-Key: <YOUR_API_KEY>' \
  -F 'target=@./statement-1.pdf' \
  -F 'target=@./statement-2.pdf' \
  -F 'document_no=STMT-2026-0001' \
  -F 'document_type=1' \
  -F 'callback_url=https://business.example.com/callbacks/amount-stat'
```

> 也可用 URL 数组提交：把 `-F 'target=@./...'` 换成 `-F 'target_urls=["https://.../statement-1.pdf","https://.../statement-2.pdf"]'`。同名字段重复多次也兼容；文件与 URL 可混合，后缀（`.pdf`）与大小限制不变。

提交响应（HTTP 202）：

```json
{
  "task_id": "a1b2c3d4e5f6",
  "document_no": "STMT-2026-0001",
  "document_type": "1",
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
  "document_type": "1",
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
  "document_type": "1",
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
  "document_type": "1",
  "error": "OCR 解析失败"
}
```

#### 管线测试（免鉴权）

与合同比对页对称，金额统计也提供免鉴权的管线测试端点，支持重复 `target` 文件和/或 `target_urls` JSON 数组（兼容重复 HTTP/HTTPS PDF URL 字段），同样接受 `document_type`（合同/对帐单）字段，供本机前端在不触发真实回调的前提下验证整条 OCR + 表格抽取 + 求和管线（强制 `document_no` 以 `API-TEST-` 开头，查询端点据此隔离真实任务）：

```bash
# 提交(无需 X-API-Key,可多选 PDF)
curl -X POST 'https://compare.example.com/api/v1/statement/api-test' \
  -F 'target=@./statement-1.pdf' \
  -F 'target=@./statement-2.pdf'

# 轮询查询结果(响应结构与对外 amountStat 查询一致)
curl 'https://compare.example.com/api/v1/statement/api-test/<TASK_ID>'
```

完整数据结构和设计取舍见 [技术方案](docs/技术方案.md)；金额统计的设计与边界见 [金额统计方案](docs/金额统计方案.md)。
