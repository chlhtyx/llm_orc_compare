# 外部金额统计 API 调用文档

对帐单/发票等 PDF 的金额统计对外接口。可一次提交**多个 PDF**,服务端串行 OCR + 表格抽取后用代码确定性求和,返回各文件、各金额列的汇总与合计校验结果。

> 与[外部合同比对 API](./external-api.md) 共用同一套鉴权、审计、任务管理、同步/异步双模式与回调机制;差异在于:输入为**多 PDF**(无原始合同)、产出为**金额汇总**(无高亮图片)、回调 `event_type` 为 `statement.summary.*`。两套 API 独立,不互相影响。

---

## 目录

- [一、通用约定](#一通用约定)
- [二、提交统计 `POST /amountStat`](#二提交统计-post-amountstat)
- [三、同步调用模式 (`sync=true`)](#三同步调用模式-synctrue)
- [四、异步调用模式 (`sync=false`,默认)](#四异步调用模式-syncfalse默认)
- [五、查询结果 `GET /amountStat/{task_id}`](#五查询结果-get-amountstattask_id)
- [六、完整字段字典](#六完整字段字典)
- [七、错误码总表](#七错误码总表)
- [八、回调(Webhook)说明](#八回调webhook说明)
- [九、端点速查](#九端点速查)

---

## 一、通用约定

与合同比对 API 完全一致,以下仅做简述,详见[外部合同比对 API → 通用约定](./external-api.md#一通用约定)。

- **Base URL**:`{external_public_base_url}/api/v1/external/amountStat`,例如 `https://dc.example.com/api/v1/external/amountStat`。
- **鉴权**:请求头 `X-API-Key: {external_api_key}`。服务端用常数时间比较校验;未配置 Key 时跳过鉴权(内网/可信环境)。缺失或不匹配 → `401 invalid external API key`。
- **请求格式**:`multipart/form-data`。文件走 `target` 字段(可重复多次),URL 走 `target_urls` 字段(可重复多次),两者可混合。
- **统一错误响应体**:所有非 2xx 返回统一结构:
  ```json
  { "code": 400, "message": "第 1 个文件必须是 .pdf", "request_id": "a1b2c3d4e5f6" }
  ```
- **响应头 `X-Request-Id`**:每次请求服务端生成 12 位 hex,同时写入审计记录,便于排障。
- **服务可用性前提**:服务端必须已配置 `external_public_base_url`(合法 HTTP/HTTPS)、`external_max_upload_mb > 0`、`external_image_dpi > 0`,否则端点返回 `503 外部 API 未配置`。

---

## 二、提交统计 `POST /amountStat`

`POST /api/v1/external/amountStat`

### 请求字段(form-data)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `target` | File(可多个) | 与 `target_urls` 至少一个 | 对帐单/发票 PDF(`.pdf`)。同名字段重复多次即可上传多个文件 |
| `target_urls` | string(可多个) | 与 `target` 至少一个 | PDF 的 URL(`http/https`,`.pdf`)。同名字段重复多次,可传多个 URL。下载超 `external_max_upload_mb` 会中断 |
| `document_no` | string | **是** | 单据号(用于回调与审计追溯)。非空,≤ 255 字符 |
| `callback_url` | string | 异步必填 / 同步可选 | 结果回调地址。规则见[合同比对文档](./external-api.md#callback_url-校验规则) |
| `sync` | bool | 否 | `true`=同步阻塞,`false`(默认)=异步受理 |

> `target` 与 `target_urls` 可混合提交,服务端按"文件段 + URL 段"顺序串行处理。至少提供一个 PDF,否则 `400`。

#### 文件大小与页数限制

- 单文件大小上限复用 `external_max_upload_mb`(默认 50 MiB,超限 → `413`)。
- 若服务端配置了 `max_pdf_pages`,每个 PDF 超过该页数会被拒绝(`400`)。

### 同步 / 异步

- `sync=false`(默认):立即返回 `202` + `task_id`,结果经回调或查询端点获取。
- `sync=true`:同步阻塞至统计完成,响应体内直接返回完整结果(`200`)。

---

## 三、同步调用模式 (`sync=true`)

### 请求示例(curl)

```bash
curl -X POST "https://dc.example.com/api/v1/external/amountStat" \
  -H "X-API-Key: YOUR_KEY" \
  -F "document_no=STMT-2026-0001" \
  -F "sync=true" \
  -F "target=@statement1.pdf" \
  -F "target=@statement2.pdf"
```

### 响应

- **成功**:HTTP `200`,响应体即[任务成功结果](#任务成功done)。
- **失败**:HTTP `200`,但 `status` 字段为 `"failed"`,错误信息在 `error` 字段(服务端捕获异常置失败,不向调用方抛 HTTP 错误)。客户端应**先读 `status` 再决定成败**,不要仅凭 HTTP 200 判定成功。

> ⚠️ 客户端超时建议设为 120s 以上(OCR + 表格抽取耗时随页数与文件数增长)。

---

## 四、异步调用模式 (`sync=false`,默认)

### 请求示例(curl)

```bash
curl -X POST "https://dc.example.com/api/v1/external/amountStat" \
  -H "X-API-Key: YOUR_KEY" \
  -F "document_no=STMT-2026-0001" \
  -F "callback_url=https://your.system/callback" \
  -F "target=@statement1.pdf"
```

### 受理响应

```json
{
  "task_id": "9f3c4a1b7d8e2e1f",
  "document_no": "STMT-2026-0001",
  "status": "pending"
}
```

- HTTP `202 Accepted`。
- `callback_url` 在异步模式下**必填**,否则 `400 异步模式必须提供 callback_url`。

### 拿结果的两种方式(任选其一,推荐都用)

- **方式 A:回调推送(被动接收)** — 任务终态时服务端 POST 到 `callback_url`,见[第八节](#八回调webhook说明)。
- **方式 B:主动查询(兜底 / 补偿)** — `GET /api/v1/external/amountStat/{task_id}`,见[第五节](#五查询结果-get-amountstattask_id)。

### 任务状态机

`pending`(受理)→ `running`(统计中)→ `done`(成功)/ `failed`(失败)。终态后不再变化。

---

## 五、查询结果 `GET /amountStat/{task_id}`

`GET /api/v1/external/amountStat/{task_id}`

### 任务未完成(`pending` / `running`)——只有基础字段

```json
{
  "task_id": "9f3c4a1b7d8e2e1f",
  "document_no": "STMT-2026-0001",
  "status": "running",
  "stage": "statement_file_1",
  "progress": 0.35,
  "error": null
}
```

### 任务成功(`done`)——基础字段 + 结果字段

```json
{
  "task_id": "9f3c4a1b7d8e2e1f",
  "document_no": "STMT-2026-0001",
  "status": "done",
  "stage": "done",
  "progress": 1.0,
  "error": null,
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
  "result_url": "https://dc.example.com/api/v1/external/amountStat/9f3c4a1b7d8e2e1f"
}
```

### 任务失败(`failed`)——只有基础字段 + error

```json
{
  "task_id": "9f3c4a1b7d8e2e1f",
  "document_no": "STMT-2026-0001",
  "status": "failed",
  "stage": "statement_aggregate",
  "progress": 0.5,
  "error": "等待执行槽位超时(...)"
}
```

### task_id 不存在 / 非本接口创建的任务 → HTTP 404

只有 `external_request=true` 的金额统计任务可查询;内部提交的金额统计任务或其它类型的任务 → `404 task not found`。

---

## 六、完整字段字典

### 基础字段(任何状态都有)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `task_id` | string | 任务 ID(16 位 hex) |
| `document_no` | string | 提交时传入的单据号 |
| `status` | string | `pending` / `running` / `done` / `failed` |
| `stage` | string | 当前阶段名(如 `statement_file_2`、`statement_aggregate`、`done`);`done`/`failed` 后为终态 stage |
| `progress` | float | 0.0 ~ 1.0 |
| `error` | string \| null | 失败原因(仅 `failed` 有值) |

### 结果字段(仅 `status == "done"` 时附带)

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `grand_total` | number | **核心输出**:所有文件、所有金额列之和(单位元) |
| `verdict` | string | 判定:`clean`(声明合计一致)/ `changed`(声明不一致)/ `needs_review`(OCR 或列定位低置信) |
| `file_totals` | array | 每个输入文件的金额合计。元素含 `file_index`、`file_name`、`total_amount` 与 `error`；单文件失败时 `total_amount` 为 0，`error` 说明原因 |
| `total_files` | int | 处理的 PDF 文件数 |
| `total_tables` | int | 识别到的表格总数 |
| `total_items` | int | 抽取到的金额行总数 |
| `reasons` | string[] | 判定理由(可审计) |
| `result_url` | string | 本任务结果查询绝对地址 |

> **算术确定性**:`grand_total` 与各级合计始终由代码用 `Decimal` 求和得出。LLM 仅在正则启发式列定位失败时兜底指认金额列(列索引),且 LLM 抽出的每个金额必须能在 OCR 文本中逐字溯源(数字归一化后子串包含),否则丢弃并标记 `needs_review`。详见[金额统计方案](./金额统计方案.md)。

### `verdict` 取值

| 值 | 含义 |
| --- | --- |
| `clean` | 数据存在且声明的合计行与代码求和一致 |
| `changed` | 存在声明合计行,但与代码求和不一致 |
| `needs_review` | OCR 质量低或金额列定位失败,结果待人工复核 |

---

## 七、错误码总表

| HTTP | 触发条件 | message |
| --- | --- | --- |
| **200** | 同步模式受理(无论任务成败) | 响应体 `status` 区分 |
| **202** | 异步模式受理 | — |
| **400** | 参数校验失败(无 PDF、后缀非 `.pdf`、`document_no` 空/超长、异步缺 `callback_url`、URL 非法、超页数、PDF 解析失败、URL 下载失败) | 校验原文 |
| **401** | `X-API-Key` 缺失或不匹配 | `invalid external API key` |
| **404** | task_id 不存在或非本接口创建 | `task not found` |
| **413** | 文件超过 `external_max_upload_mb` | `第 N 个文件超过 ... MiB 上限` |
| **422** | form 字段类型错误 / 必填缺失(如 `document_no` 整个没传、`sync` 非布尔) | Pydantic 校验原文 |
| **429** | 全局并发任务数已达上限 | `并发任务已达上限,请稍后重试` |
| **500** | 未捕获的内部异常 | `internal error: {详情}` |
| **503** | 服务端未完成外部 API 配置 | `外部 API 未配置` / `外部 API 上传大小配置无效` |

### 重试建议

- **401 / 400 / 404 / 422**:不要重试,属于调用方问题。
- **413**:换更小的文件。
- **429**:退避重试(如指数退避 1s → 2s → 4s)。
- **5xx**:可重试,属服务端临时故障。
- **同步 `200 + status="failed"`**:读 `error` 判断;若是资源类(超时、并发),可重新提交。

---

## 八、回调(Webhook)说明

异步模式下,任务进入终态(`done` 或 `failed`)时,服务端会向你提交的 `callback_url` 发起 **POST**(与合同比对 webhook 一致:指数退避重试 1s/4s/16s、最多 3 次)。

### 请求

```http
POST <你的 callback_url>
Content-Type: application/json
X-Event-Id: {event_id}
```

- `X-Event-Id`:事件唯一 ID(UUID 格式)。同一任务在同一终态下可能因重试投递多次发送同一 `event_id`,接收方应据此做幂等去重。

### 成功事件 payload(`status="done"`)

```json
{
  "event_id": "b7e1...-...-...",
  "task_id": "9f3c...e1",
  "status": "done",
  "event_type": "statement.summary.completed",
  "document_no": "STMT-2026-0001",
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
  "result_url": "https://dc.example.com/api/v1/external/amountStat/9f3c...e1"
}
```

> 回调 payload 与查询端点 `done` 响应字段完全一致(去掉 `event_id`/`task_id`/`status` 信封后)。

### 失败事件 payload(`status="failed"`)

```json
{
  "event_id": "...",
  "task_id": "9f3c...e1",
  "status": "failed",
  "event_type": "statement.summary.failed",
  "document_no": "STMT-2026-0001",
  "error": "等待执行槽位超时(...)"
}
```

> 同步模式(`sync=true`)下,若同时提供 `callback_url`,回调会在后台发送,**不阻塞** HTTP 响应。

---

## 九、端点速查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/external/amountStat` | 提交金额统计(同步 `200` / 异步 `202`) |
| `GET` | `/api/v1/external/amountStat/{task_id}` | 查询状态与结果 |
| `POST` | `/api/v1/statement/api-test` | **管线测试**(免鉴权,不触发回调,强制 `API-TEST-` 前缀) |
| `GET` | `/api/v1/statement/api-test/{task_id}` | 查询管线测试结果(响应结构同上,仅放行 `API-TEST-` 任务) |

> 管线测试端点(`api-test`)与对外端点对称,接受重复 `target` 文件和/或重复 `target_urls`(HTTP/HTTPS PDF URL),用于本机前端在不触发真实回调、不需要 `X-API-Key` 的前提下验证整条 OCR + 表格抽取 + 求和管线。提交时 `document_no` 由服务端自动生成为 `API-TEST-{随机串}`,查询端点据此隔离真实业务任务(非 `API-TEST-` 前缀 → `404`)。这些端点不属于 `/api/v1/external/*` 前缀,**不**入审计中间件、**不**计入外部调用审计表。
