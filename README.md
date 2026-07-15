# 文档比对系统（llm_orc_compare）

基于多模态 LLM API 的合同篡改检测系统。以原始 Word 合同为基准，比对回收的 PDF（文本 PDF 或扫描件），识别金额、日期、违约责任、管辖法院等条款是否发生变化，并生成可追溯的差异报告。

## 主要能力

- 解析 Word 正文和表格，并保留条款结构。
- 优先读取 PDF 原生文本；扫描页自动调用多模态 OCR。
- 支持 OpenAI 兼容的多模态模型，也可切换到独立部署的 PaddleOCR-VL HTTP 服务。
- 通过编号、字段和语义向量对齐条款，执行字符级及表格单元格级 diff。
- 对金额、日期、主体、责任等高风险要素进行强校验，并可启用独立的纯文本 LLM 复核。
- 提供标准条款比对和纯文本快速比对两种模式。
- 通过 SSE 实时展示任务进度，并支持任务完成 Webhook。
- 输出 JSON 报告、带差异标注的 PDF，以及带整段高亮的 Word 报告。
- 报告页可在线查看原始 Word 全文及差异高亮；PDF 有坐标信息时可同步查看 PDF 标注。

## 快速开始

推荐使用 Docker Compose，前端和后端会运行在同一个容器中。

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
curl http://localhost:8000/health
```

默认访问地址为 <http://localhost:8000>。当前 `docker-compose.yml` 使用 `${DC_PORT:-3012}` 作为宿主机端口；复制 `.env.example` 后，端口为其中配置的 `DC_PORT=8000`。

首次使用时：

1. 打开页面右上角的“设置”。
2. 配置 OCR 服务的 API Base、API Key 和模型名。
3. 按需配置语义向量服务和 LLM 复核服务。
4. 返回提交页，上传原始 `.docx` 和待核验 `.pdf`。
5. 等待任务完成后查看差异，并下载高亮 Word、PDF 或 JSON 报告。

配置通过 UI 写入 `./data/llm_config.json`，上传文件、报告和日志也保存在 `./data/`，容器重启后不会丢失。不要提交包含真实 API Key 的配置文件。

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
4. **差异检测**：组合字符级 diff、表格单元格 diff、语义相似度和高风险要素校验。
5. **语义复核（可选）**：将疑似修改条款交给独立的纯文本 LLM 判断语义影响。
6. **质量门禁与报告**：识别质量异常时避免输出不可靠的高风险结论，最终生成 JSON、PDF 和 DOCX 报告。

## 配置说明

LLM/OCR 配置统一在 UI 设置页维护，并持久化到 `llm_config.json`：

| 配置组 | 用途 | 可选后端 |
| --- | --- | --- |
| OCR | 扫描页文字与版面识别 | `llm`、`paddleocr` |
| Embedding | 无编号条款的语义对齐 | `qwen`、`bge`、`mock` |
| Judge | 对疑似修改条款做语义复核 | OpenAI 兼容的纯文本模型 |

服务端基础配置仍通过环境变量提供：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DC_HOST` | `0.0.0.0` | 后端监听地址 |
| `DC_PORT` | `8000` | 后端监听端口；Compose 中也用作宿主机映射端口 |
| `DC_STORAGE_DIR` | `./.dc_data` | 上传、报告、配置和日志目录 |
| `DC_STATIC_DIR` | 空 | 前端静态文件目录；容器内已设为 `/app/static` |
| `DC_LOG_LEVEL` | `INFO` | 日志级别，可设为 `DEBUG` |

## 目录结构

```text
.
├── docs/
│   ├── deploy.md                 # Docker 部署与运维
│   └── 技术方案.md               # 完整技术设计
├── src/document_comparison/
│   ├── api/                      # FastAPI 接口
│   ├── parsing/                  # Word / PDF 解析
│   ├── ocr/                      # LLM / PaddleOCR 引擎及质量检查
│   ├── structure/                # 条款切分与归一化
│   ├── align/                    # 条款对齐
│   ├── compare/                  # diff、风险要素与 LLM 复核
│   ├── embed/                    # qwen / bge / mock 向量引擎
│   ├── report/                   # JSON、PDF、DOCX 报告
│   ├── pipeline.py               # 标准条款比对流水线
│   └── raw_pipeline.py           # 纯文本快速比对流水线
├── web/                          # Vue 3 + Vite + Pinia 前端
├── tests/                        # pytest 测试
├── scripts/                      # 流水线性能基准脚本
├── Dockerfile                    # 前后端多阶段构建
└── docker-compose.yml            # 单容器部署
```

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/v1/compare` | 提交标准条款比对 |
| `GET` | `/api/v1/compare/{task_id}` | 查询任务状态和结果 |
| `GET` | `/api/v1/compare/{task_id}/events` | 订阅 SSE 进度 |
| `GET` | `/api/v1/compare/{task_id}/report?format=json\|pdf\|docx` | 下载报告 |
| `GET` | `/api/v1/compare/{task_id}/docx-preview` | 获取 Word 全文及差异标记 |
| `POST` | `/api/v1/raw-compare` | 提交纯文本快速比对 |
| `GET` | `/api/v1/config/llm` | 获取当前模型配置（Key 脱敏） |
| `PUT` | `/api/v1/config/llm` | 更新并持久化模型配置 |

完整数据结构和设计取舍见 [技术方案](docs/技术方案.md)。
