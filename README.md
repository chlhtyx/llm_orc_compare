# 文档比对系统（llm_orc_compare）

基于 **多模态 LLM API** 的合同条款篡改检测：以原始 Word 合同为基准，自动比对 PDF 扫描件，识别条款是否被篡改。

## 它解决什么问题

原始 Word 合同与回收盖章的 PDF 扫描件之间，金额、日期、违约责任、管辖法院等条款可能被暗中改动。人工逐字核对成本高、易遗漏，长合同尤其明显。本系统把两份文档都解析成结构化条款，自动对齐、比对，并输出篡改风险报告。

## 核心思路

1. **Word 解析**（python-docx）→ 结构化条款（含表格结构化）
2. **PDF 扫描件 OCR**（多模态 VL 模型 API 或 PaddleOCR-VL）→ 结构化条款
3. **条款对齐**：编号锚定 + 字段锚定 + 语义向量匹配
4. **比对检测**：字符级 diff + 表格单元格 diff + 语义相似度 + 高风险要素强校验
5. **LLM 复核**（可选）：对疑似修改条款调纯文本 LLM 语义复核
6. **报告**：结构化 JSON + PDF 高亮报告

## 文档

完整技术方案见 [docs/技术方案.md](docs/技术方案.md)。

## 目录结构

```
.
├── docs/
│   └── 技术方案.md        # 技术设计文档
├── src/document_comparison/   # 后端（FastAPI + 流水线）
│   ├── parsing/           # Word(python-docx) / PDF(PyMuPDF) 解析
│   ├── ocr/               # OCR 引擎层（LLMOCREngine / PaddleOCREngine，可切换）
│   ├── structure/         # 条款切分与归一化
│   ├── align/             # 条款对齐（编号+字段+语义四层兜底）
│   ├── compare/           # 比对（diff/elements/risk/judge）
│   ├── embed/             # 向量引擎（qwen/bge/mock）
│   ├── report/            # 报告生成（JSON + PDF 高亮烧录）
│   ├── api/               # FastAPI 服务
│   ├── pipeline.py        # 主流水线编排
│   └── raw_pipeline.py    # 纯文本 difflib 流水线（无标注版）
├── web/                   # 前端（Vue 3 + Vite + Pinia）
│   └── src/
│       ├── views/         # 提交页 / 报告页 / 设置页 / 纯文本比对页
│       ├── components/    # PdfViewer / DiffItem / HunkCard ...
│       ├── stores/        # Pinia（task / modelConfig）
│       └── api/           # 封装后端接口
├── tests/                 # pytest 测试
├── scripts/               # 性能基准脚本
├── Dockerfile             # 多阶段构建（前端 SPA + 后端）
└── docker-compose.yml     # 单容器部署
```

## 环境要求

- Python ≥ 3.9（开发用 3.12）
- OCR 通过多模态 LLM API（OpenAI 兼容）完成，无需本地 GPU / PaddlePaddle
- 双 OCR 引擎可切换：
  - `llm`（默认）：Qwen3-VL、GPT-4o 等多模态对话模型
  - `paddleocr`：PaddleOCR-VL（专用 OCR 模型，解析 LOC 坐标标记）

## 状态

核心已实现并已容器化部署。包含：Word/PDF 解析、双 OCR 引擎、条款对齐、字符级 + 表格单元格级 diff、高风险要素校验、LLM 复核、JSON + PDF 高亮报告、Vue3 前端。

