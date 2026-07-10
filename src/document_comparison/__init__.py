"""文档比对系统。

模块布局对齐技术方案 docs/技术方案.md:
- models        §6 数据结构
- parsing       ① 文档解析层
- ocr           ② OCR 文档理解层(接口 + mock + LLM API)
- structure     ③ 结构化抽取层
- align         ④ 条款对齐层
- compare       ⑤ 比对与篡改检测层
- report        ⑥ 报告生成层
- embed         语义向量(对齐兜底,接口 + mock)
- pipeline      流水线编排
- auth/webhook/tasks/storage/api  服务层(§7、§14)
"""

__version__ = "0.1.0"
