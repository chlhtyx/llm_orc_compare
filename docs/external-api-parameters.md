# 合同比对与金额统计 API 调用参数

本文按当前代码核对，适用于默认本机地址 `http://localhost:3012`。远程调用时将 `localhost` 换成部署服务器地址；文件 URL 和回调地址必须能被服务端访问，在容器中 `localhost` 指容器自身。

## 1. 公共约定

| 用途 | 方法 | 地址 |
| --- | --- | --- |
| 提交合同比对 | POST | `http://localhost:3012/api/v1/external/contractCompare` |
| 提交金额统计 | POST | `http://localhost:3012/api/v1/external/amountStat` |
| 查询合同比对 | GET | `http://localhost:3012/api/v1/external/contractCompare/{task_id}` |
| 查询金额统计 | GET | `http://localhost:3012/api/v1/external/amountStat/{task_id}` |

- 提交使用 **`multipart/form-data`**。文件为二进制上传，其他参数为表单文本；不要把整个请求体作为 JSON，也不要传文件 Base64。
- 配置了 `external_api_key` 时，提交、查询和报告/图片下载均须携带请求头 `X-API-Key`；未配置 Key 时免鉴权。外部接口不要求控制台登录 Cookie。
- 服务端须配置有效的 `external_public_base_url`，用于生成结果链接；该值不是本次请求参数，也不一定等于调用时的地址。
- 默认每个文件最大 50 MiB，以服务端 `external_max_upload_mb` 实际配置为准。PDF 页数受 `max_pdf_pages` 限制，默认 `0` 表示不限。
- 本文示例中的文件、单据号、URL、Key 均为占位示例，执行前替换。curl 使用 `-F` 自动生成 multipart boundary，不要手动覆盖 `Content-Type`。

### 两个提交接口共有的表单字段

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `document_no` | string | 是 | 无 | 业务单据号，去除首尾空格后须非空，最多 255 字符；通过返回的 `task_id` 标识本次任务 |
| `sync` | boolean | 否 | `false` | `false` 异步；`true` 同步等待。建议表单值写 `true` / `false` |
| `callback_url` | string | 条件必填 | 无 | `sync=false` 时必填；`sync=true` 时可省略，提供时仍会回调。须为有效 HTTP/HTTPS 地址，不能含 URL 用户名/密码 |

**同步不保证返回 200**：`sync=true` 持续排队超过 `DC_SYNC_QUEUE_WAIT_SECONDS`（默认 300 秒）会返回 HTTP 202 和任务状态，任务继续排队，调用方须凭 `task_id` 查询。该预算只限制排队等待，不是整个任务执行超时。`sync=false` 成功受理返回 HTTP 202；同步等待到终态返回 HTTP 200，仍须检查业务 `status`。

## 2. 合同比对参数

`POST /api/v1/external/contractCompare`

除公共字段外，支持以下字段：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `source` | file | 与 `source_url` 二选一 | 原始合同，支持 `.docx`、`.pdf` |
| `source_url` | string | 与 `source` 二选一 | 服务端下载原始合同的 HTTP/HTTPS URL，下载文件须为 `.docx` 或 `.pdf` |
| `target` | file | 与 `target_url` 二选一 | 回收合同，仅支持 `.pdf` |
| `target_url` | string | 与 `target` 二选一 | 服务端下载回收合同的 HTTP/HTTPS URL，下载文件须为 `.pdf` |
| `original_page_count` | integer | 否 | 原始合同真实页数，须 ≥ 1；仅在服务端启用按原件页数截取回收件时使用，单独传入不会开启截取 |
| `target_body_end_page` | integer | 否 | 回收件正文截止页，页码从 1 开始，须 ≥ 1 且不超过回收件实际页数；传入后仅比对回收件第 1 页至该页，后续页被排除 |

每一侧必须且只能选文件或 URL，但两侧可以采用不同方式，例如 `source` + `target_url`。URL 不允许包含用户名/密码。

页数截取优先级：显式 `target_body_end_page` → 服务端自动排除尾部图纸 → 服务端按原件页数截取。`original_page_count` 只在最后一种策略下生效。OCR、LLM 和风险评估等由服务端外部接口配置控制，不是此接口的表单参数。

### 文件上传：同步

```bash
export BASE_URL='http://localhost:3012'
export API_KEY='替换为外部接口API Key'

curl -i "$BASE_URL/api/v1/external/contractCompare" \
  -H "X-API-Key: $API_KEY" \
  -F 'document_no=HT-20260917-001' \
  -F 'source=@/path/to/original.docx' \
  -F 'target=@/path/to/returned.pdf' \
  -F 'sync=true'
```

需要显式排除正文之后的附件时，可增加 `-F 'target_body_end_page=10'`（示例要求回收 PDF 至少 10 页）。

### URL 下载：异步

```bash
curl -i "$BASE_URL/api/v1/external/contractCompare" \
  -H "X-API-Key: $API_KEY" \
  -F 'document_no=HT-20260917-002' \
  --form-string 'source_url=https://files.example.com/original.pdf' \
  --form-string 'target_url=https://files.example.com/returned.pdf' \
  --form-string 'callback_url=https://caller.example.com/callback/compare' \
  -F 'sync=false'
```

异步受理示例（HTTP 202）：

```json
{
  "task_id": "example-compare-task-id",
  "document_no": "HT-20260917-002",
  "status": "pending"
}
```

## 3. 金额统计参数

`POST /api/v1/external/amountStat`

除公共字段外，支持以下字段：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `target` | file[] | 与 `target_urls` 合计至少一个文件 | 空 | 一个或多个 PDF；每个文件使用同名 `target` 字段重复提交 |
| `target_urls` | string[] | 与 `target` 合计至少一个文件 | 空 | 一个或多个 PDF 的 HTTP/HTTPS URL；允许与上传文件混合，字段名是复数 `target_urls` |
| `document_type` | string | 否 | `"1"` | `1` = 发票，`2` = 对帐单；兼容 `发票`、`对帐单`、`对账单`，空值按 `1` 处理；其他值返回 400 |

`target_urls` 支持重复同名字段，也支持在一个表单字段中放 JSON 字符串数组（整个 HTTP 请求仍为 multipart）。不使用逗号拼接 URL。空 URL 会被忽略；上传文件与 URL 同时存在时，文件处理顺序为上传文件在前、URL 下载文件在后。统计输入均为 PDF，不支持直接上传图片或 Excel。

### 多文件上传：同步统计对帐单

```bash
curl -i "$BASE_URL/api/v1/external/amountStat" \
  -H "X-API-Key: $API_KEY" \
  -F 'document_no=DZ-20260917-001' \
  -F 'document_type=2' \
  -F 'target=@/path/to/statement-1.pdf' \
  -F 'target=@/path/to/statement-2.pdf' \
  -F 'sync=true'
```

### 文件和 URL 混合：异步统计发票

```bash
curl -i "$BASE_URL/api/v1/external/amountStat" \
  -H "X-API-Key: $API_KEY" \
  -F 'document_no=FP-20260917-001' \
  -F 'document_type=1' \
  -F 'target=@/path/to/invoice-1.pdf' \
  --form-string 'target_urls=https://files.example.com/invoice-2.pdf' \
  --form-string 'target_urls=https://files.example.com/invoice-3.pdf' \
  --form-string 'callback_url=https://caller.example.com/callback/amount' \
  -F 'sync=false'
```

以上两个 `target_urls` 字段也可替换为：

```bash
--form-string 'target_urls=["https://files.example.com/invoice-2.pdf","https://files.example.com/invoice-3.pdf"]'
```

异步受理示例（HTTP 202）：

```json
{
  "task_id": "example-amount-task-id",
  "document_no": "FP-20260917-001",
  "document_type": "1",
  "status": "pending"
}
```

## 4. 查询及结果字段

将示例任务 ID 替换为提交接口实际返回值：

```bash
curl "$BASE_URL/api/v1/external/contractCompare/example-compare-task-id" \
  -H "X-API-Key: $API_KEY"

curl "$BASE_URL/api/v1/external/amountStat/example-amount-task-id" \
  -H "X-API-Key: $API_KEY"
```

查询成功 HTTP 200。两个接口共有 `task_id`、`document_no`、`status`、`stage`、`progress`（0～1）、`error`；金额统计另有 `document_type`。轮询遇到 `pending` / `running` 时继续等待，遇到 `done` / `failed` / `stopped` 时停止。可按 5、10、20 秒逐步延长查询间隔。跨进程或重启后查询时，`stage` 可能为空、`progress` 可能只有终态值，应以 `status` 为准。

### 合同比对完成时追加

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `change_status` | string | `clean` 未发现变化；`changed` 发现变化；`needs_review` 待人工复核 |
| `result_text` | string | 可读的比对结果与差异说明 |
| `highlight_images` | string[] | 回收件逐页高亮 PNG 地址，可能为空 |
| `source_highlight_images` | string[] | 原始合同逐页高亮 PNG 地址，可能为空；DOCX 使用派生 PDF |
| `result_url` | string | PDF 报告下载地址 |
| `html_url` | string | 自包含 HTML 报告下载地址 |

报告或图片生成异常不会撤销已完成任务；图片列表可能为空，报告文件缺失时下载可能返回 404。下载同样遵循 `X-API-Key` 鉴权，例如：

```bash
curl -f "$BASE_URL/api/v1/external/contractCompare/example-compare-task-id/report.pdf" \
  -H "X-API-Key: $API_KEY" -o comparison-report.pdf
```

### 金额统计完成时追加

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `grand_total` | number | 所有文件汇总金额 |
| `verdict` | string | `clean` 校验通过；`changed` 声明金额与计算结果不一致；`needs_review` 待人工复核 |
| `file_totals` | object[] | 每文件含 `file_index`、`file_name`、`total_amount`、`error` |
| `total_files` / `total_tables` / `total_items` | integer | 文件数、表格数、金额项数 |
| `reasons` | string[] | 判定理由 |
| `result_url` | string | 本任务金额统计 JSON 查询地址 |

金额由代码确定性求和。表格抽取无可用金额时会追加整页多模态核对；任一文件仍无可溯源金额则任务为 `failed`，在 `error` 中说明原因，不返回部分合计或以 0 代替缺失金额。明确识别的 0 元可正常返回。当前外部金额统计结果只返回上述扁平汇总，**不包含完整 `report` 字段**。

## 5. 回调与错误处理

配置 `callback_url` 后，服务端向该地址 POST JSON；完成事件分别为 `contract.compare.completed`、`statement.summary.completed`，失败事件分别为 `contract.compare.failed`、`statement.summary.failed`。回调包含任务标识、状态以及对应结果或错误，金额统计含单据类型。接收方应按 `X-Event-Id` 去重并及时返回 2xx；当前回调没有 HMAC 签名。可用查询接口补偿回调未送达的情况。

| HTTP 状态 | 含义 / 处理 |
| --- | --- |
| 200 | 同步任务到终态或查询成功；检查 JSON `status`，不能直接认定业务成功 |
| 202 | 异步已受理，或同步排队超过等待预算；保存 `task_id` 后查询 |
| 400 | 参数组合冲突、单据号/页数/类型/URL 非法、异步缺回调、PDF 解析失败或超页数；URL 下载失败及下载超大小上限也返回 400 |
| 401 | 已配置 Key，但请求缺少或传错 `X-API-Key` |
| 404 | 任务不存在或不可通过该外部查询接口访问，或下载产物不存在 |
| 413 | 直接上传的单个文件超过大小上限 |
| 422 | 缺必填参数或参数类型无法解析（例如整数、布尔值非法） |
| 429 | 等待队列已满，稍后重试 |
| 503 | 外部 API 配置无效、服务暂停接单或任务入队失败 |
| 500 | 服务内部异常，保留请求标识交由维护人员排查 |

错误体示例：

```json
{
  "code": 400,
  "message": "异步模式必须提供 callback_url",
  "request_id": "example-request-id"
}
```

外部接口响应头提供 `X-Request-Id`，便于关联审计记录。拿到 `task_id` 后优先查询该任务，避免重复提交产生新的处理任务。

## 6. Postman 填写方式

1. 方法选 `POST`，填写第 1 节中的接口地址。
2. Headers 按配置添加 `X-API-Key`。
3. Body 选 `form-data`，`source` / `target` 的类型选 **File**，其余字段选 **Text**。
4. 金额统计多文件使用多个同名 `target` 行；多个 URL 使用多个同名 `target_urls` 行。
5. 同步测试填 `sync=true`；异步测试填 `sync=false` 并填写可从服务端访问的 `callback_url`。保留 Postman 自动生成的 `Content-Type`。

字段依据：`src/document_comparison/api/app.py` 的两个提交端点与结果查询函数、`src/document_comparison/external_api.py` 的鉴权及结果构造函数。本文为源码核对文档，不代表已对本机 3012 服务完成联调。
