# AGENTS.md

## 项目概览

这是一个合同篡改检测系统：以原始 `.docx` 为基准，比对回收的 `.pdf`，生成 JSON、PDF 和 DOCX 差异报告。后端为 Python/FastAPI，前端为 Vue 3/Vite/Pinia，Docker 镜像将两者打包为单一服务。

## 目录与职责

- `src/document_comparison/api/`：FastAPI 路由、SSE 进度和配置接口。
- `src/document_comparison/pipeline.py`：结构化条款比对主流水线。
- `src/document_comparison/raw_pipeline.py`：纯文本快速比对流水线；不要与主流水线混为一谈。
- `src/document_comparison/parsing/`、`ocr/`：Word/PDF 解析与 OCR 降级策略。
- `src/document_comparison/structure/`、`align/`、`compare/`：条款切分、对齐、差异和风险判定。
- `src/document_comparison/report/`：JSON、PDF 和 DOCX 报告输出。
- `web/`：Vue 前端；接口客户端在 `web/src/api/`，状态在 `web/src/stores/`。
- `tests/`：后端 pytest 测试。

## 关键行为约束

- 字符级/表格级差异和 canonical 强校验是变化判定的依据；语义向量只用于条款对齐。
- LLM 辅助说明只能补充已确认差异的解释或严重度建议，不能撤销确定的变化。
- 原生 PDF 文本优先；仅在需要时降级 OCR，并保留低质量结果为“待复核”的语义。
- 改动报告数据结构时，保持 JSON、PDF、DOCX 输出及前端展示的一致性。
- 改动 API 合约时，同步检查 `web/src/api/`、相关 stores、视图和后端测试。

## 开发与验证

后端（Python 3.9+）：

```bash
pip install -e '.[dev]'
pytest
```

前端（Node.js 20+）：

```bash
cd web
npm ci
npm run type-check
npm run build
```

按改动范围运行相关测试；涉及跨端接口、报告格式或流水线行为时，同时运行后端测试和前端类型检查/构建。

## 配置与安全

- 不要提交 `.env`、`data/`、`llm_config.json`、上传文件、报告、日志或任何真实 API Key。
- 模型/OCR 配置由 UI 持久化；服务端运行配置使用 `DC_*` 环境变量。
- 新增外部模型、OCR 或向量服务时，遵循现有可替换后端模式，并为不可用或低质量结果提供明确降级行为。

## 变更原则

- 保持改动小而聚焦；不要顺带重构无关模块。
- 为修复或新行为补充/更新测试，特别是金额、日期、主体、账号、责任及表格单元格的差异场景。
- 更新用户可见的工作流、配置项或 API 时，同步更新 `README.md` 或 `docs/` 中对应说明。
