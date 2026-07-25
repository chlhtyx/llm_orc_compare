# 文档比对 · 前端（Vue 3 + Vite + Pinia + TS）

对接后端 [FastAPI](../src/document_comparison/api/app.py)，提供「提交比对 → 实时进度 → 篡改报告」的完整界面。

## 快速开始

```bash
cd web
npm install
npm run dev        # http://localhost:5173
```

开发期 `/api/*` 与 `/health` 由 [vite.config.ts](vite.config.ts) 代理到 `http://localhost:8000`，因此需**先起后端**：

```bash
# 项目根目录
python -m document_comparison.api.app
```

## 脚本

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 开发服务器（含后端代理） |
| `npm run build` | 类型检查 + 生产构建到 `dist/` |
| `npm run preview` | 预览构建产物 |
| `npm run type-check` | 仅类型检查（vue-tsc） |

## 目录结构

```
src/
├── api/
│   ├── types.ts       # 与后端 pydantic 模型一一对应
│   ├── client.ts      # fetch 封装:错误归一化、fetch-Stream SSE
│   └── compare.ts     # 端点封装(/health、/compare、/events、/report)
├── stores/
│   ├── task.ts        # 提交、SSE 进度(轮询兜底)、终结态
│   └── report.ts      # 报告派生统计(按风险分级、要素变更)
├── router/            # / 提交页 · /report/:taskId 报告页
├── views/             # SubmitView · ReportView
├── components/        # FileDrop · ProgressTracker · OverallBadge
│                      # DiffList/DiffItem(字符级)· KeyElementTable · PdfViewer
└── styles/main.css    # 设计令牌 + 通用样式
```

## 与后端契约的对应

- 进度流：`GET /api/v1/compare/{id}/events` 为 SSE，用 `fetch` + `ReadableStream` 解析 `data:` 帧；流失败时自动降级为 `getTask` 轮询。
- 报告：`TamperReport` 的 TS 类型见 [types.ts](src/api/types.ts)，字段与后端 [models.py](../src/document_comparison/models.py) 完全对齐。

## 待办（已知留空）

- **PdfViewer 精确叠加**：当前用原生 `<iframe>` 预览 + 高亮区域清单。要做到按 `page_meta` 反算 viewport、在 PDF 上叠加 `page_regions` 高亮，需引入 `pdf.js`（见后端技术方案 §5.6）。
- **PDF 报告下载**：后端 `format=pdf` 烧录高亮尚未实现，前端仅提供 `format=json` 读取。
