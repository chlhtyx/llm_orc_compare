# AGENTS.md

## 项目概览

这是一个合同篡改检测系统：以原始 `.docx` 为基准，比对回收的 `.pdf`，生成 JSON、PDF 和 DOCX 差异报告。后端为 Python/FastAPI，前端为 Vue 3/Vite/Pinia，Docker 镜像将两者打包为单一服务,数据库为 Postgres(任务记录、里程碑事件、报告 JSONB)。

## 目录与职责

- `src/document_comparison/api/`：FastAPI 路由、SSE 进度和配置接口;`/api/v1/tasks*` 提供比对记录列表与里程碑时间线查询。
- `src/document_comparison/pipeline.py`：结构化条款比对主流水线。
- `src/document_comparison/raw_pipeline.py`：纯文本快速比对流水线；不要与主流水线混为一谈。
- `src/document_comparison/statement_pipeline.py`：对帐单金额统计流水线(多文件串行);与 `raw_pipeline` 完全独立。
- `src/document_comparison/db/`：SQLAlchemy ORM、引擎工厂、任务记录/里程碑事件 CRUD;Postgres 为硬依赖,未配置 `DATABASE_URL` 启动失败。
- `alembic/`：数据库迁移脚本;生产用 `alembic upgrade head`,开发可用 `DC_DB_AUTO_CREATE=1`。
- `src/document_comparison/statement/`：金额列定位、代码确定性求和、LLM 兜底列指认。
- `src/document_comparison/parsing/`、`ocr/`：Word/PDF 解析与 OCR 降级策略。
- `src/document_comparison/structure/`、`align/`、`compare/`：条款切分、对齐、差异和风险判定。
- `src/document_comparison/report/`：JSON、PDF 和 DOCX 报告输出。
- `web/`：Vue 前端；接口客户端在 `web/src/api/`，状态在 `web/src/stores/`。
- `tests/`：后端 pytest 测试。

## 关键行为约束

- 字符级/表格级差异和 canonical 强校验是变化判定的依据；语义向量只用于条款对齐。
- LLM 辅助说明只能补充已确认差异的解释或严重度建议，不能撤销确定的变化。
- 对帐单金额统计通道中,**LLM 仅用于列定位兜底(指认金额列索引),金额抽取与求和始终由代码完成**;不可让 LLM 做算术或撤销确定性结论。
- 原生 PDF 文本优先；仅在需要时降级 OCR，并保留低质量结果为“待复核”的语义。
- 改动报告数据结构时，保持 JSON、PDF、DOCX 输出及前端展示的一致性。
- 改动 API 合约时，同步检查 `web/src/api/`、相关 stores、视图和后端测试。
- Postgres 双写规则:任务记录元数据 + 完整报告 JSONB + **仅里程碑事件**入库(白名单见 `tasks.py::_MILESTONE_STAGES`);高频进度事件只留在内存供 SSE 消费。写库失败只记日志,不抛、不阻塞任务主流程。改 ORM 模型时必须同步新增 Alembic 迁移并通过 `alembic check`。
- 标准合同比对(`pipeline.py` / `build_report`)默认 `enable_risk_assessment=False`:仅列举字符级/表格级差异,不做风险分级、不抽取高风险要素、LLM 辅助说明不触发。`Diff.risk_level` 恒为 `"none"`、`risk_reasons` 空、`key_elements` 空;`status`/`verdict`/`confidence` 与 OCR 待复核标记照常产出;`overall_risk` 从 `change_status` 推导(changed→low、needs_review、clean)。`options.enable_risk_assessment=true` 时恢复完整风险逻辑。无标注比对与对帐单统计不受此开关影响。

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
