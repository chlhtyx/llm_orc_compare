# AGENTS.md

## 项目概览

这是一个合同篡改检测系统：以原始 `.docx` 或 `.pdf` 为基准，比对回收的 `.pdf`，生成 JSON、PDF 和 DOCX 差异报告。后端为 Python/FastAPI，前端为 Vue 3/Vite/Pinia，Docker 镜像将两者打包为单一服务,数据库为 Postgres(任务记录、里程碑事件、报告 JSONB)。原始合同(source)支持 Word(`.docx`,结构化解析,带标题层级)与 PDF(`.pdf`,复用原生文本层优先 + OCR 兜底通道);回收件(target)固定为 `.pdf`。

## 目录与职责

- `src/document_comparison/api/`：FastAPI 路由、SSE 进度和配置接口;`/api/v1/tasks*` 提供比对记录列表、里程碑时间线、模型调用明细与外部接口调用审计查询;`/api/v1/stats/daily` 提供每日调用统计(控制台看板,`repository.daily_stats` 按北京时间按天聚合任务/模型调用/外部接口调用计数);`_external_audit_middleware` 为 `/api/v1/external/*` 入站请求异步落库审计记录。
- `src/document_comparison/pipeline.py`：结构化条款比对主流水线。
- `src/document_comparison/raw_pipeline.py`：纯文本快速比对流水线；不要与主流水线混为一谈。
- `src/document_comparison/statement_pipeline.py`：金额统计流水线(多文件串行);与 `raw_pipeline` 完全独立。
- `src/document_comparison/observability.py`：阶段耗时 + 模型调用日志,也是**对话型 LLM 调用记录的单点拦截入口**(`log_model_request/response/failure` 经 `current_llm_collector` contextvar 收集)。
- `src/document_comparison/db/`：SQLAlchemy ORM、引擎工厂、任务记录/里程碑事件/LLM 配置/LLM 调用记录/外部接口调用审计 CRUD;Postgres 为硬依赖,未配置 `DATABASE_URL` 启动失败。
- `alembic/`：数据库迁移脚本;生产用 `alembic upgrade head`,开发可用 `DC_DB_AUTO_CREATE=1`。
- `src/document_comparison/statement/`：金额列定位、代码确定性求和、LLM 兜底列指认。
- `src/document_comparison/parsing/`、`ocr/`：Word/PDF 解析与 OCR 降级策略;`_BoundedConcurrency` 子线程经 `ctx.run` 显式传播 `current_llm_collector` contextvar。
- `src/document_comparison/structure/`、`align/`、`compare/`：条款切分、对齐、差异和风险判定。
- `src/document_comparison/report/`：JSON、PDF 和 DOCX 报告输出。
- `web/`：Vue 前端；接口客户端在 `web/src/api/`，状态在 `web/src/stores/`。
- `tests/`：后端 pytest 测试。

## 关键行为约束

- 字符级/表格级差异和 canonical 强校验是变化判定的依据；语义向量只用于条款对齐。
- LLM 辅助说明只能补充已确认差异的解释或严重度建议，不能撤销确定的变化。
- 金额统计通道中,**LLM 仅用于列定位兜底(指认金额列索引)与金额抽取兜底**(`statement/llm_amount_extract.py`,仅当正则对该表抽空时触发,典型为发票纯数字无单位金额);**LLM 抽出的每个金额必须 grounding 校验能在 OCR 文本中逐字溯源(数字归一化后子串包含),否则丢弃→needs_review**;**金额抽取仍以代码为主(带元/万元/¥/中文大写的对帐单走正则,免费确定),求和始终由代码用 `Decimal` 完成**;不可让 LLM 做算术或撤销确定性结论。
- 原生 PDF 文本优先；仅在需要时降级 OCR，并保留低质量结果为“待复核”的语义。
- 改动报告数据结构时，保持 JSON、PDF、DOCX 输出及前端展示的一致性。
- 改动 API 合约时，同步检查 `web/src/api/`、相关 stores、视图和后端测试。
- Postgres 双写规则:任务记录元数据 + 完整报告 JSONB + **仅里程碑事件**入库(白名单见 `tasks.py::_MILESTONE_STAGES`);高频进度事件只留内存(供执行 worker 本进程查询)。写库失败只记日志,不抛、不阻塞任务主流程。改 ORM 模型时必须同步新增 Alembic 迁移并通过 `alembic check`。
- 对话型 LLM 调用记录(`task_llm_calls` 表):每次 HTTP 调用(含重试中间态、失败 attempt)各一行,经 `observability.current_llm_collector` contextvar 收集,任务结束批量入库。**只收集对话型 kind**(ocr/ocr-whole/paddleocr/judge/llm-diff/statement-column/statement-amount),**embedding 不入**(文本→向量,非对话型)。payload 中图片 base64 由 `_safe_model_value` 脱敏为 `{data_url, base64_chars, sha256}`,response 截断 64KB。threading 子线程必须用 `ctx.run` 显式传播 contextvar(见 `_BoundedConcurrency.submit/_run`);新增 OCR/judge 类调用点时,失败分支必须补 `log_model_failure` 调用,否则失败 attempt 不会有记录。
- 外部接口入站调用审计(`external_api_calls` 表):`/api/v1/external/*` 前缀的每次入站请求(提交/查询/图片/报告下载,含 401 鉴权失败、413/422 校验失败)各一行,由 `api/app.py::_external_audit_middleware` 中间件在请求结束时 `asyncio.create_task` **异步**落库(绝不阻塞响应,写库失败只记日志)。`task_id` 可空且**不加外键**——任务创建前的失败调用(401/422)需独立留存,任务删除不级联清除审计。四个合同比对外部端点(submit/result/image/report)通过 `request.state.audit_endpoint/audit_task_id/audit_document_no` 传递语义标签;中间件统一生成 `request_id`(写 `request.state` + 响应头 `X-Request-Id`),三个 exception_handler 优先复用 `request.state.request_id`。`api_key_sha256` 只存指纹不存明文。**请求参数快照**(`request_params` JSONB):端点在函数体**入口**把解析后的请求参数写入 `request.state.audit_params`(提交类=表单字段+callback_url/sync 等,**文件只记文件名不记内容**;图片类=page_number),越早写入越好,后续 400/413/429 校验失败的记录仍携带;中间件落库前经 `_sanitize_audit_params` 清洗(敏感 key 脱敏+单字符串截断 512)。**失败拦截兜底**:401(鉴权依赖)与 422(FastAPI 参数校验)端点体不执行,审计中间件在 call_next 之前经 `_prefill_external_audit_params` 预读兜底——必须整体 `await request.body()`(Starlette `_CachedRequest` 仅 body() 缓存供下游重放;stream() 会给下游发空 body 破坏表单解析),再用 python-multipart 低层回调(`_MultipartFieldCollector`)流式提取非文件字段与文件名(文件内容丢弃,单字段截断 64KB);端点体执行时覆盖为结构化快照。防内存放大:chunked(无 Content-Length)或 body 超过 `external_max_upload_mb` 的请求跳过预读记 null。新增 `/api/v1/external/*` 端点时,必须在端点内设置 `request.state.audit_endpoint`(否则审计记 `endpoint="unknown"`),有额外请求参数则同步设置 `request.state.audit_params`。
- 控制台口令鉴权(`console_auth.py` + `api/app.py::_console_auth_middleware`):`DC_CONSOLE_PASSWORD` 非空时,控制台业务接口(`/api/v1/*` 任务/报告/配置/审计查询 + `/docs`)需先 `POST /api/v1/auth/login` 换会话 Cookie(`dc_console_session`,HttpOnly + SameSite=Lax,无状态 HMAC 令牌 = 签发时间戳+签名,密钥由口令派生,多 worker 免共享,换口令即全会话失效);TTL 由 `DC_CONSOLE_SESSION_TTL_HOURS`(默认 12h)控制。**豁免**:`/api/v1/external/*`、`/api/v1/compare/api-test`(走 X-API-Key)、`/health`、`/api/v1/version`、`/api/v1/auth/*`、SPA 静态资源;注意 `/api/v1/external-calls`(控制台审计查询)**受保护**。登录失败按 IP 进程内限速(5 分钟 10 次→429)。中间件注册于 scan_protection 之内(401 仍带安全头且进 access log,登录 body 的 `password` key 已被 access log 脱敏)。前端:路由守卫每次跳转查 `/api/v1/auth/status`,`client.ts` 对控制台路径 401 统一跳 `/login` 回跳原地址(auth/external/api-test 的 401 是业务错误不跳转)。新增控制台业务端点**自动**被中间件覆盖,无需逐端点加 Depends;但新增外部端点若不在 `/api/v1/external/` 前缀下需同步更新 `console_auth._EXEMPT_PATHS` 与前端 `CONSOLE_401_EXEMPT`。
- 外部比对 HTML/PDF 报告产物:外部合同比对任务完成时,除高亮 PNG 外,额外生成自包含 HTML 报告(`external_api.write_external_html_report` → `reports_dir/{task_id}_external_report.html`,渲染在 `report/html_report.py`)与同内容 PDF 版本(`write_external_pdf_report` → `{task_id}_external_report.pdf`,渲染在 `report/pdf_report.py`,pymupdf 直接拼版 + 内置 `china-s` 字体,概要+差异明细+逐页高亮图)。**结果字段**:`result_url` 指向 PDF 报告(`GET /api/v1/external/contractCompare/{task_id}/report.pdf`),`html_url` 指向 HTML 报告(`.../report.html`);两处均为 attachment 下载,文件名 `【单据号】对比YYYYMMDD-HHMM.{pdf,html}`(北京时间为完成时刻到分钟;on-disk 仍按 task_id 命名)。HTML 报告含结构化差异表格 + 已烧录差异标注的每页 PNG(base64 data URI 内嵌,单文件离线可看,不依赖图片端点)。两份报告渲染失败都只记日志不阻塞任务主流程。JSON 查询端点 `GET /contractCompare/{task_id}` 保留不变。
- 入站 API 请求/响应日志(access log,写本地滚动日志 `storage_dir/logs/app.log`,不入库):`api/app.py::ApiAccessLogMiddleware` 是**纯 ASGI 中间件**(不是 `@app.middleware("http")`/BaseHTTPMiddleware——后者会把所有下游响应重包成 `_StreamingResponse`,中间件返回时 `response.body` 不可读,无法捕获响应体)。它通过拦截 ASGI `receive`/`send` 通道抓取请求体与响应体块,对 `/api/` 与 `/health` 前缀生效,记录 method/path/query/status/耗时 + 完整请求体与响应体。**边界**:multipart 文件上传只记摘要(不读 docx/pdf 二进制);SSE(`text/event-stream`)与文件下载(FileResponse)不缓存响应体。JSON 内容对命中 `password/api_key/token/secret/authorization` 的 key 脱敏为 `***`,但**合同条款原文、金额等业务内容按配置完整记录**(会进入本地日志)。开关:`DC_API_REQUEST_LOGGING`(默认 `1` 开启),单条 body 上限 `DC_API_LOG_MAX_BODY`(默认 64KB,超出截断加 `…(truncated)`)。**风险提示**:若部署环境的日志会被第三方(日志收集服务)读取,需额外评估业务敏感数据暴露,可置 `DC_API_REQUEST_LOGGING=0` 关闭或降低 `DC_API_LOG_MAX_BODY`。
- 出站模型/OCR 调用的完整请求/响应体日志:`observability.py::log_model_request/response/failure` 在原有 INFO 摘要行(sha256+字符数,不含原文,**不变**)之外,新增 **DEBUG 级**输出完整脱敏请求/响应体(图片 base64 仍按 sha256 摘要)。默认 `DC_LOG_LEVEL=INFO` 只看摘要;排查模型输入/返回原文时设 `DC_LOG_LEVEL=DEBUG` 即可在 app.log 看到 `model request body=...`/`model response body=...`。**embedding 响应是纯向量数组**(体积大、无可读业务语义),DEBUG 级也跳过响应体(只留摘要),避免撑爆日志。
- 日志【业务-步骤】标注(`logging_config._FMT` 的 `【%(biz_tag)s】`):业务 = `log_context(task_kind=...)`(TaskManager 注入 compare/raw/statement),步骤 = `log_context(step=...)`。步骤由 `tasks.py::_make_progress_cb` 在进度 stage 变化时经 `observability.set_log_fields(step=_step_label(stage))` 即时切换——`set_log_fields` 是无作用域版 `log_context`,set 只影响当前线程 context(pipeline 单工作线程顺序执行 + OCR 孙线程 `ctx.run` 快照),主协程任务级日志保持仅业务名。映射 `_step_label` 与 `_STAGE_TIMING_KEY` 计时桶共用主映射(word/ocr/structure/align/compare/normalize/diff),对帐单细分为 start/file-N/aggregate,任务级事件(start/done/failed)返回 None 不切换。新增流水线阶段时同步更新 `_STAGE_TIMING_KEY` 与 `_step_label`。
- 多 worker 部署(无 Redis 依赖,全部走 Postgres 共享状态):`DC_UVICORN_WORKERS=N` 经 gunicorn + `UvicornWorker` 起 N 个进程,默认 1=单进程。跨 worker 协作三点:**①全局并发上限**走 `db_repo.count_active_tasks`(PG `task_records.status` 计数),5 处 429 限流统一用它,执行端另有 `asyncio.Semaphore(settings.max_concurrent_tasks)`(`tasks.py::TaskManager._sem`)作进程内第二道保险;`settings.max_concurrent_tasks` 由 `DC_MAX_CONCURRENT_TASKS` 配置。**②SSE**(`tasks.py::TaskManager.event_stream`)改从 PG `task_events` 增量拉取(`get_task_events_after` + `get_task_status`),粒度为里程碑(约 6-10 节点/任务),不再读进程内 `Task.events`;3 个 SSE 端点内存 miss 时回查 PG `get_task` 取 status。**③DB 连接池**每个 worker 独立持有(`pool_size+max_overflow`,默认 15/worker),`DC_DB_POOL_*` 配置;多 worker 需确认 PG `max_connections`。`gunicorn.conf.py` 设 `timeout=0`(比对任务长跑不被杀)、`preload_app=False`(`TaskManager` 单例 + `asyncio.Semaphore` 不可跨 fork 共享,每 worker 独立创建)。`callback_secret` 已移除——webhook 不再 HMAC 签名(`webhook.deliver` 无 secret 参,`sign`/`verify` 删除),回调仅带 `X-Event-Id`。
- 标准合同比对(`pipeline.py` / `build_report`)默认 `enable_risk_assessment=False`:仅列举字符级/表格级差异,不做风险分级、不抽取高风险要素、LLM 辅助说明不触发。`Diff.risk_level` 恒为 `"none"`、`risk_reasons` 空、`key_elements` 空;`status`/`verdict`/`confidence` 与 OCR 待复核标记照常产出;`overall_risk` 镜像 `change_status`(changed→changed、needs_review、clean),**不再出现 low/medium/high**;前端不再显示风险徽章,报告页统一用 `change_status` 文案(发现确认内容变化 / 存在待人工复核 / 未发现内容变化),per-diff 风险标签仅在 `risk_level !== 'none'` 时才渲染。`apply_recognition_gate` 默认 `enable_risk_assessment=False`,与 build_report 保持一致。`options.enable_risk_assessment=true` 时恢复完整风险逻辑(高/中/低风险分级 + per-diff 徽章)。无标注比对与对帐单统计不受此开关影响。

## 开发与验证

后端（Python 3.9+）：

```bash
pip install -e '.[dev]'
pytest
```

> 运行后端测试前,需要本机或 CI 提供一个 Postgres,并设置:
> - `DATABASE_URL`:应用启动连接的开发库(模型 import 时即建表)
> - `DATABASE_URL_TEST`:可选,测试隔离库(避免污染开发库);未设置时 db 测试自动跳过
>
> 例如:`export DATABASE_URL="postgresql+psycopg://dc:dcpass@localhost:5432/doc_compare"`

前端（Node.js 20+）：

```bash
cd web
npm ci
npm run type-check
npm run build
```

按改动范围运行相关测试；涉及跨端接口、报告格式或流水线行为时，同时运行后端测试和前端类型检查/构建。

## 配置与安全

- 不要提交 `.env`、`data/`、`llm_config.json`(旧文件,升级后保留作备份,仍可能含真实 Key)、上传文件、报告、日志或任何真实 API Key。
- 模型/OCR 配置由 UI 持久化到 Postgres `llm_config` 表;服务端运行配置使用 `DC_*` 环境变量。首次启动若旧 `llm_config.json` 存在且 PG 无记录,会自动一次性导入文件内容并保留文件作备份。
- 新增外部模型、OCR 或向量服务时，遵循现有可替换后端模式，并为不可用或低质量结果提供明确降级行为。

## 变更原则

- 保持改动小而聚焦；不要顺带重构无关模块。
- 为修复或新行为补充/更新测试，特别是金额、日期、主体、账号、责任及表格单元格的差异场景。
- 更新用户可见的工作流、配置项或 API 时，同步更新 `README.md` 或 `docs/` 中对应说明。
