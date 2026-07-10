# 文档比对系统（Document Comparison）

基于 **PaddleOCR-VL-1.5** 的合同条款篡改检测：以原始 Word 合同为基准，自动比对 PDF 扫描件，识别条款是否被篡改。

## 它解决什么问题

原始 Word 合同与回收盖章的 PDF 扫描件之间，金额、日期、违约责任、管辖法院等条款可能被暗中改动。人工逐字核对成本高、易遗漏，长合同尤其明显。本系统把两份文档都解析成结构化条款，自动对齐、比对，并输出篡改风险报告。

## 核心思路

1. **Word 解析**（python-docx）→ 结构化条款
2. **PDF 扫描件 OCR**（PaddleOCR-VL-1.5）→ 结构化条款（含表格、印章）
3. **条款对齐**：编号锚定 + 语义向量匹配
4. **比对检测**：字符级 diff + 语义相似度 + 高风险要素强校验
5. **报告**：结构化 JSON + PDF 高亮报告

## 文档

完整技术方案见 [docs/技术方案.md](docs/技术方案.md)，涵盖架构、流程、数据结构、API、篡改策略、难点、评估与里程碑。

## 目录结构（规划）

```
.
├── docs/
│   └── 技术方案.md        # 技术设计文档（当前）
├── src/                   # 后端源码（待实现）
│   ├── parsing/           # Word/PDF 解析
│   ├── ocr/               # PaddleOCR-VL-1.5 封装
│   ├── structure/         # 条款切分与归一化
│   ├── align/             # 条款对齐
│   ├── compare/           # 比对与篡改检测
│   ├── report/            # 报告生成
│   └── api/               # FastAPI 服务
├── web/                   # 前端（Vue 3 + Vite，待实现）
│   └── src/
│       ├── views/         # 提交页 / 报告页
│       ├── components/    # PdfViewer / ClauseList / DiffView ...
│       ├── stores/        # Pinia（task / report）
│       └── api/           # 封装后端接口
├── tests/                 # 测试与评估
└── README.md
```

## 环境要求（OCR 引擎）

- Python 3.9–3.13
- PaddlePaddle ≥ 3.2.1（NVIDIA GPU 需 CC ≥ 7.0）
- `pip install -U "paddleocr[doc-parser]"`
- 生产推荐：VLM 推理服务（vLLM / SGLang / FastDeploy）+ 官方 Docker 镜像

## 状态

技术方案阶段。下一步按里程碑 M1 搭建环境与 OCR 基线。
