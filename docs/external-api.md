# 外部合同比对 API 调用文档

供外部系统接入「合同篡改检测」服务使用。以原始 `.docx` 合同为基准,比对回收的 `.pdf`(盖章件 / 扫描件),返回内容变化结论、逐条差异、以及逐页高亮图片。

支持**同步**和**异步**两种调用方式。

---

## 目录

- [一、通用约定](#一通用约定)
- [二、提交比对 `POST /contractCompare`](#二提交比对-post-contractcompare)
- [三、同步调用模式 (`sync=true`)](#三同步调用模式-synctrue)
- [四、异步调用模式 (`sync=false`)](#四异步调用模式-syncfalse)
- [五、查询结果 `GET /contractCompare/{task_id}`](#五查询结果-get-contractcomparetask_id)
- [六、获取高亮图片 `GET /contractCompare/{task_id}/images/{page}`](#六获取高亮图片-get-contractcomparetask_idimagespage)
- [七、完整字段字典](#七完整字段字典)
- [八、错误码总表](#八错误码总表)
- [九、回调(Webhook)说明](#九回调webhook说明)
- [十、枚举值速查](#十枚举值速查)
- [十一、排障:request_id 与审计](#十一排障request_id-与审计)

---

## 一、通用约定

### Base URL

```
{external_public_base_url}/api/v1/external
```

`external_public_base_url` 由本系统的服务管理员在前端「合同比对 API」配置页填写并对外公布(例如 `https://compare.example.com`)。本文档后续示例统一用 `https://compare.example.com` 代替。

### 鉴权:请求头 `X-API-Key`

```http
X-API-Key: <由服务管理员分配的高强度随机 Key>
```

- **所有外部端点**都需要带这个头。
- 若服务端**未配置 Key**(内网 / 可信环境),可省略此头,服务端跳过鉴权直接放行。
- 若服务端**配置了 Key**,则此头缺失或不匹配 → **HTTP 401**。
- 服务端用常数时间比较(`hmac.compare_digest`),且审计日志只记录 Key 的 SHA-256 指纹,不存明文。

> 是否配置了 Key、以及 Key 的值,都由服务管理员管理,外系统只需向其索取。详见服务方的配置说明。

### 请求格式

提交端点含文件上传,故**必须用 `multipart/form-data`**(不是 `application/json`)。查询和图片端点为普通 GET。

### 统一错误响应体

任何错误(无论哪个端点)都返回如下结构:

```json
{
  "code": 400,
  "message": "可读的错误说明",
  "request_id": "a1b2c3d4e5f6"
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `code` | int | HTTP 状态码,与响应的 HTTP status 一致 |
| `message` | string | 人类可读的错误原因(中文或英文,取决于具体校验点) |
| `request_id` | string | 12 位十六进制请求标识,可用于事后排障关联审计记录(见[十一](#十一排障request_id-与审计)) |

成功响应没有统一外壳,各端点有自己的结构(见后文)。

### 响应头 `X-Request-Id`

每个外部请求的响应都会带 `X-Request-Id` 头,值与错误体里的 `request_id` 一致。

### 服务可用性前提

服务端必须已正确配置(管理员在前端配置页完成):

- `external_public_base_url`:合法的 HTTP(S) 地址
- `external_max_upload_mb > 0`
- `external_image_dpi > 0`

任一不满足 → 所有外部端点直接返回 **HTTP 503 `外部 API 未配置`**,与 Key 是否正确无关。如果遇到 503,请联系服务管理员完成配置。

---

## 二、提交比对 `POST /contractCompare`

```
POST https://compare.example.com/api/v1/external/contractCompare
Content-Type: multipart/form-data
X-API-Key: <KEY>
```

### 请求字段(form-data)

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `source` | file | 与 `source_url` 二选一 | — | 原始合同文件,**文件名必须以 `.docx` 结尾**(大小写不敏感)。仅按后缀判定,不看 MIME。 |
| `source_url` | string | 与 `source` 二选一 | — | 原始合同 URL(`http`/`https`)。服务端下载后比对,文件名取 `Content-Disposition` 或 URL 末段,**后缀仍必须 `.docx`**。大小限制同 `source`。 |
| `target` | file | 与 `target_url` 二选一 | — | 回收件文件,**文件名必须以 `.pdf` 结尾**。必须可被正常解析,否则 400。 |
| `target_url` | string | 与 `target` 二选一 | — | 回收件 URL(`http`/`https`)。服务端下载后比对,文件名取 `Content-Disposition` 或 URL 末段,**后缀仍必须 `.pdf`**。大小限制同 `target`。 |
| `document_no` | string | ✅ | — | 单据号 / 合同编号。会去除首尾空白后校验:不能为空、长度 ≤ 255 字符。用于结果回显与审计关联。 |
| `sync` | bool | ❌ | `false` | 调用模式开关。`true`=同步(见[三](#三同步调用模式-synctrue));`false`=异步(见[四](#四异步调用模式-syncfalse))。 |
| `callback_url` | string | ⚠️ 条件必填 | `null` | **异步模式必填**,同步模式可选。回调地址,校验规则见下。 |
| `original_page_count` | int | ❌ | `null` | 原始合同的真实页数,**仅当服务端开启了「回收件页数截取」时才生效**。用于在 PDF 末尾有多多余图纸页时,显式指定截取到第几页。提供时必须 ≥ 1。 |

> **文件与链接二选一**:每个角色(`source`/`target`)在文件与 `_url` 字段之间**只能传一个**;同时传或都不传 → **400**(`source 与 source_url 只能二选一` / `必须提供 source 文件或 source_url`)。两种模式下游比对完全一致。

#### `callback_url` 校验规则

- scheme 必须是 `http` 或 `https`,且必须有主机名(hostname)。
- **不允许**在 URL 中携带用户名密码(如 `http://user:pass@host`)。
- 校验失败 → **400**:`callback_url 必须是有效的 HTTP/HTTPS 地址` 或 `callback_url 不允许包含用户名或密码`。
- 异步模式(`sync=false`)下未提供 → **400**:`异步模式必须提供 callback_url`。

> `source_url` / `target_url` 沿用同样的 scheme / userinfo 校验(scheme 仅 `http`/`https`、禁止带账号密码)。

#### 文件大小与页数限制

- 单文件上限由服务端的 `external_max_upload_mb` 控制(默认 50 MiB,可由管理员调整),**分别**限制 `source` 和 `target`。超限 → **413**:`{source|target} 超过 {N} MiB 上限`。
- URL 模式下,服务端**流式下载并累计字节**,超过同一上限立即中断 → **400**:`{source|target} 超过 {N} MiB 下载上限`(下载大小口径与文件上传一致,不另设)。
- 若服务端开启了 PDF 页数上限(`max_pdf_pages > 0`),提交时会先解析 PDF 页数(文件模式读 `target.file`,URL 模式读下载产物),超页 → **400**:`暂不支持:PDF 共 {N} 页,超过上限 {M} 页`。

#### URL 下载行为(仅 `*_url` 字段)

- 超时 `DC_DOWNLOAD_TIMEOUT_SECONDS`(默认 60s)、最大重定向 `DC_DOWNLOAD_MAX_REDIRECTS`(默认 5)。
- 下载失败(非 2xx、超时、连接错误、超限)→ **400**,错误信息**只带状态码和主机名**,不回显完整 URL(避免泄露可能存在的 token)。
- 文件名推断:`Content-Disposition: filename*=` → `filename=` → URL path 末段 → 角色名兜底;扩展名从文件名取,无则 `.bin`。

### 提交流程

1. 校验角色二选一 / 文件类型 / 字段 / 大小 / 页数(URL 模式先下载再校验后缀与页数)。
2. 校验全局并发(见下)。
3. 创建任务(生成 `task_id`),保存上传文件或落定下载产物,启动比对。
4. 根据 `sync` 走同步等待分支,或立即返回受理响应。

#### 并发限制

服务端有全局并发任务上限。提交时若当前运行中任务数已达上限 → **429**:`并发任务已达上限,请稍后重试`。建议外系统对 429 做退避重试。

---

## 三、同步调用模式 (`sync=true`)

> 适合**单次、即时、低频**的调用。调用方发起请求后**一直阻塞**,直到比对全部完成(解析 → OCR → 对齐 → 差异 → 渲染高亮图),然后**在同一个 HTTP 响应里直接拿到完整结果**。

### 如何调用

在 form-data 里设 `sync=true`。`callback_url` 此时可省略(即便提供,回调也会在后台异步补发,**不阻塞**本次响应)。

### 请求示例(curl)

```bash
curl -X POST https://compare.example.com/api/v1/external/contractCompare \
  -H "X-API-Key: YOUR_KEY" \
  -F "source=@/path/original.docx" \
  -F "target=@/path/recovered.pdf" \
  -F "document_no=HT-2026-0001" \
  -F "sync=true"
```

### 响应

**始终返回 HTTP 200**(见下方重要说明),响应体为完整任务结果:

```json
{
  "task_id": "9f3c...e1",
  "document_no": "HT-2026-0001",
  "status": "done",
  "stage": "done",
  "progress": 1.0,
  "error": null,
  "change_status": "changed",
  "result_text": "单据号：HT-2026-0001\n结论：发现确认内容变化\n...",
  "highlight_images": [
    "https://compare.example.com/api/v1/external/contractCompare/9f3c...e1/images/1",
    "https://compare.example.com/api/v1/external/contractCompare/9f3c...e1/images/2"
  ],
  "result_url": "https://compare.example.com/api/v1/external/contractCompare/9f3c...e1/report.pdf",
  "html_url": "https://compare.example.com/api/v1/external/contractCompare/9f3c...e1/report.html"
}
```

字段含义见[七、完整字段字典](#七完整字段字典)。

### ⚠️ 重要:同步模式如何判断成败

**同步模式下,即使比对内部失败,HTTP 状态码也是 200**,不会是 5xx。判断成败**必须读响应体的 `status` 字段**:

| `status` | 含义 | 响应体特征 |
|---|---|---|
| `"done"` | 比对成功完成 | 附带 `change_status` / `result_text` / `highlight_images` / `result_url` / `html_url` 五个字段 |
| `"failed"` | 比对执行失败 | `error` 字段带失败原因;**不**附带上述四个结果字段 |

即:`code/message/request_id` 那套错误结构**只覆盖入参校验类失败**(发生在比对启动之前,如 400/401/413/429)。比对执行过程中的失败(如 OCR、LLM 超时)不会触发 5xx,而是以 `200 + status="failed"` 返回。

### ⚠️ 客户端超时设置

**同步模式没有服务端整体超时上限**——会一直等到 pipeline 跑完。典型耗时从数十秒到数分钟不等(取决于 PDF 页数、是否走 OCR、是否启用 LLM 辅助)。

**调用方务必把 HTTP 客户端的读取超时设得足够长**(建议 ≥ 5 分钟,并按实际文件规模上调)。否则客户端会先于服务端断连,虽然服务端仍会完成比对,但调用方拿不到结果(只能后续改用查询端点凭 `task_id` 取回)。

> **推荐做法**:不确定耗时的场景优先用异步模式。同步模式适合文件规模可控、且客户端超时可设足够长的场景。

---

## 四、异步调用模式 (`sync=false`,默认)

> 适合**批量、高频、长耗时**的调用。提交后立即拿到 `task_id`,比对在后台进行;通过**回调推送**或**主动查询**两种方式之一获取最终结果。

### 如何调用

`sync` 不传或传 `false`,并**必须**提供 `callback_url`。

### 请求示例(curl)

```bash
curl -i -X POST https://compare.example.com/api/v1/external/contractCompare \
  -H "X-API-Key: YOUR_KEY" \
  -F "source=@/path/original.docx" \
  -F "target=@/path/recovered.pdf" \
  -F "document_no=HT-2026-0001" \
  -F "sync=false" \
  -F "callback_url=https://your-system.example.com/hooks/compare"
```

> 也可改用链接提交(每角色文件与 URL 二选一),把 `source`/`target` 换成 `source_url`/`target_url`:
> ```bash
> -F "source_url=https://files.example.com/path/original.docx" \
> -F "target_url=https://files.example.com/path/recovered.pdf"
> ```

### 受理响应

**HTTP 202 Accepted**(注意不是 200):

```json
{
  "task_id": "9f3c1a2b3c4d5e6f",
  "document_no": "HT-2026-0001",
  "status": "pending"
}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `task_id` | string | 任务 ID。**后续查询、取图、关联回调都靠它**,请妥善保存。 |
| `document_no` | string | 回显你提交的单据号。 |
| `status` | string | 固定 `"pending"`,表示已受理、排队中。 |

### 拿结果的两种方式(任选其一,推荐都用)

#### 方式 A:回调推送(被动接收,推荐主用)

比对完成(或失败)时,服务端会向你的 `callback_url` 发起 `POST`。详见[九、回调(Webhook)说明](#九回调webhook说明)。

#### 方式 B:主动查询(兜底 / 补偿)

凭 `task_id` 轮询 `GET /contractCompare/{task_id}`。建议配合**指数退避**(如每 5/10/20 秒一次,直到 `status` 变为终态 `done` / `failed`)。详见[五](#五查询结果-get-contractcomparetask_id)。

> **幂等说明**:回调与查询返回的结果结构一致(同一个 `build_external_result` 生成),可安全地把「回调到达」和「主动查询」互为补偿:回调丢了就靠查询兜底,查询频次太高就以回调为主。

### 任务状态机

任务 `status` 只有 4 个取值:

```
pending ──> running ──> done      (成功)
                └──> failed       (失败)
```

- `pending`:已受理,等待执行(可能在排队拿并发槽位)。
- `running`:正在执行(解析 / OCR / 对齐 / 比对 / 渲染)。
- `done`:成功完成,可读取完整结果。
- `failed`:执行失败,读 `error` 字段获取原因。

**没有 `cancelled` 状态**。`pending` / `running` 为中间态,`done` / `failed` 为终态。一旦进入终态不再变化。

---

## 五、查询结果 `GET /contractCompare/{task_id}`

```
GET https://compare.example.com/api/v1/external/contractCompare/{task_id}
X-API-Key: <KEY>
```

适用于:异步模式轮询结果、同步模式断连后凭 `task_id` 补取结果。

### 响应

**HTTP 200**,响应体结构随 `status` 而变:

#### 任务未完成(`pending` / `running`)——只有基础字段

```json
{
  "task_id": "9f3c...e1",
  "document_no": "HT-2026-0001",
  "status": "running",
  "stage": "ocr_done",
  "progress": 0.42,
  "error": null
}
```

此时**没有** `change_status` 等结果字段。

#### 任务成功(`done`)——基础字段 + 结果字段

```json
{
  "task_id": "9f3c...e1",
  "document_no": "HT-2026-0001",
  "status": "done",
  "stage": "done",
  "progress": 1.0,
  "error": null,
  "change_status": "changed",
  "result_text": "...",
  "highlight_images": ["..."],
  "result_url": ".../report.pdf",
  "html_url": ".../report.html"
}
```

#### 任务失败(`failed`)——只有基础字段 + error

```json
{
  "task_id": "9f3c...e1",
  "document_no": "HT-2026-0001",
  "status": "failed",
  "stage": "failed",
  "progress": 0.0,
  "error": "等待执行槽位超时(30.0s)"
}
```

#### task_id 不存在 / 非本接口创建的任务 → HTTP 404

```json
{ "code": 404, "message": "task not found", "request_id": "..." }
```

> 进程重启后的历史任务:只要数据库里还有记录,仍可查询,但 `stage` 会回退为空串、`progress` 在 `done` 时为 1.0、其它状态为 0.0(因中间进度只存内存)。

---

## 六、获取高亮图片 `GET /contractCompare/{task_id}/images/{page}`

```
GET https://compare.example.com/api/v1/external/contractCompare/{task_id}/images/{page_number}
X-API-Key: <KEY>
```

获取比对时生成的逐页高亮 PNG(已把差异标注烧录进 PDF 再渲染成图片)。

### 路径参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 任务 ID |
| `page_number` | int | 页码,**从 1 开始**(首页 = 1)。`page_number < 1` → 404。 |

### 响应

**HTTP 200**,返回二进制 PNG 图片:

- `Content-Type: image/png`
- `Content-Disposition: inline; filename="{task_id}-page-{page:04d}.png"`

直接作为图片渲染或下载即可。

### 关于图片列表

完整页码列表请从查询端点或回调 payload 的 `highlight_images` 字段获取——它是一个绝对 URL 数组,每项指向一页:

```json
"highlight_images": [
  "https://compare.example.com/api/v1/external/contractCompare/{task_id}/images/1",
  "https://compare.example.com/api/v1/external/contractCompare/{task_id}/images/2"
]
```

数组长度等于实际生成的图片页数(动态探测文件存在性生成),不存在越界页。

### 错误情况

| 场景 | HTTP | message |
|---|---|---|
| `page_number < 1` | 404 | `image not found` |
| `task_id` 不存在 / 非本接口创建 | 404 | `task not found` |
| 页码超出实际页数 / 图片未生成 | 404 | `image not found` |

> 图片在比对成功(`done`)时一次性生成,查询端点只读静态文件。因此:**任务未 `done` 时取图会 404**;任务 `done` 后即便服务重启,图片仍可访问。

---

## 七、完整字段字典

### 任务结果字段(查询端点 / 同步响应 / 回调 payload 共用)

#### 基础字段(任何状态都有)

| 字段 | 类型 | 含义 |
|---|---|---|
| `task_id` | string | 任务唯一标识。 |
| `document_no` | string | 提交时传入的单据号;未传则为空串 `""`。 |
| `status` | string | 任务状态。枚举见[十](#十枚举值速查)。 |
| `stage` | string | 当前里程碑阶段名(如 `start`/`ocr_done`/`compare_done`/`done`/`failed`)。任务落库后查(如进程重启后)为空串。**用于进度展示,不应作为业务判断依据**——业务判断只用 `status`。 |
| `progress` | float | 进度,`0.0` ~ `1.0`。落库后查询时:`done` 为 `1.0`,其它为 `0.0`。 |
| `error` | string \| null | 失败原因;成功时为 `null`。 |

#### 结果字段(仅 `status == "done"` 时附带)

| 字段 | 类型 | 含义 |
|---|---|---|
| `change_status` | string | **核心结论**。内容是否变化。枚举见[十](#十枚举值速查)。 |
| `result_text` | string | 人类可读的中文结论文本,多行(`\n` 分隔)。格式见下。 |
| `highlight_images` | string[] | 逐页高亮 PNG 的**绝对 URL** 数组,基于 `external_public_base_url`。 |
| `result_url` | string | 自包含 **PDF** 比对报告下载端点的绝对 URL(概要+差异明细+逐页高亮图,attachment)。 |
| `html_url` | string | 自包含 HTML 比对报告下载端点的绝对 URL(同内容 HTML 版本,attachment,离线单文件)。 |

### `result_text` 文本格式

纯文本,`\n` 换行。结构如下:

**头部固定 5 行**:

```
单据号：{document_no}
结论：{change_status 的中文}
识别状态：{recognition_status 的中文}
高亮定位：{location_status 的中文}
差异数量：{N}
```

**每条差异 3 行**(从 1 开始编号):

```
[{index}] {条款编号 标题}（{差异类型中文}）
原始合同：{原文 或 "（无）"}
回收件：{回收文 或 "（无）"}
```

示例:

```
单据号：HT-2026-0001
结论：发现确认内容变化
识别状态：可靠
高亮定位：完整
差异数量：2
[1] 第三条 付款方式（修改）
原始合同：乙方应于合同签订后 30 日内付款
回收件：乙方应于合同签订后 15 日内付款
[2] 补充条款（新增）
原始合同：（无）
回收件：本合同有效期至 2027 年 12 月 31 日
```

---

## 八、错误码总表

所有错误响应体统一为 `{code, message, request_id}`。

| HTTP | 触发场景 | `message` 示例 |
|---|---|---|
| **400** | `source` 非 `.docx` | `source 必须为 .docx` |
| **400** | `target` 非 `.pdf` | `target 必须为 .pdf` |
| **400** | `document_no` 为空 | `document_no 不能为空` |
| **400** | `document_no` 超 255 字符 | `document_no 不能超过 255 个字符` |
| **400** | `original_page_count < 1` | `original_page_count 必须 >= 1` |
| **400** | 异步模式未提供 `callback_url` | `异步模式必须提供 callback_url` |
| **400** | `callback_url` 非法 | `callback_url 必须是有效的 HTTP/HTTPS 地址` / `callback_url 不允许包含用户名或密码` |
| **400** | PDF 无法解析 | `无法解析 PDF: {详情}` |
| **400** | PDF 页数超上限 | `暂不支持:PDF 共 {N} 页,超过上限 {M} 页` |
| **400** | `source`/`source_url` 或 `target`/`target_url` 同时传 / 都不传 | `source 与 source_url 只能二选一` / `必须提供 source 文件或 source_url` |
| **400** | `source_url`/`target_url` scheme 非法或带账号密码 | `source_url/target_url 必须是有效的 HTTP/HTTPS 地址` / `...不允许包含用户名或密码` |
| **400** | URL 下载失败(非 2xx / 超时 / 连接失败) | `下载 {source\|target} 失败:HTTP {code} ({host})` / `下载 {role} 超时 ({host})` |
| **400** | URL 下载体积超限 | `{source\|target} 超过 {N} MiB 下载上限` |
| **401** | `X-API-Key` 缺失或不匹配(服务端已配置 Key 时) | `invalid external API key` |
| **404** | `task_id` 不存在 / 非本接口创建 | `task not found` |
| **404** | 图片页码越界 / 未生成 | `image not found` |
| **413** | 单个文件超过大小上限 | `{source\|target} 超过 {N} MiB 上限` |
| **422** | form 字段类型错误 / 必填缺失(如 `document_no` 整个没传、`sync` 非布尔) | Pydantic 校验原文 |
| **429** | 全局并发任务数已达上限 | `并发任务已达上限,请稍后重试` |
| **500** | 未捕获的内部异常 | `internal error: {详情}` |
| **503** | 服务端未完成外部 API 配置 | `外部 API 未配置` / `外部 API 上传大小配置无效` |

### 重试建议

- **401 / 400 / 404 / 422**:不要重试,属于调用方问题,需修正请求。
- **413**:换更小的文件,或联系管理员调高上限。
- **429**:退避重试(如指数退避 1s → 2s → 4s)。
- **5xx**:可重试,属服务端临时故障。
- **同步 `200 + status="failed"`**:读 `error` 判断;若是资源类(超时、并发),可重新提交。

---

## 九、回调(Webhook)说明

异步模式下,任务进入终态(`done` 或 `failed`)时,服务端会向你提交的 `callback_url` 发起 **POST**。

### 请求

```http
POST <你的 callback_url>
Content-Type: application/json
X-Event-Id: {event_id}
```

- `X-Event-Id`:事件唯一 ID(UUID 格式)。**同一任务在同一终态下可能因重试投递多次发送同一 `event_id`**,接收方应据此做幂等去重。

### 成功事件 payload(`status="done"`)

```json
{
  "event_id": "b7e1...-...-...",
  "task_id": "9f3c...e1",
  "status": "done",
  "event_type": "contract.compare.completed",
  "document_no": "HT-2026-0001",
  "change_status": "changed",
  "result_text": "...",
  "highlight_images": ["..."],
  "result_url": ".../report.pdf",
  "html_url": ".../report.html"
}
```

### 失败事件 payload(`status="failed"`)

```json
{
  "event_id": "...",
  "task_id": "9f3c...e1",
  "status": "failed",
  "event_type": "contract.compare.failed",
  "document_no": "HT-2026-0001",
  "error": "失败原因..."
}
```

### 字段说明

| 字段 | 类型 | 含义 |
|---|---|---|
| `event_id` | string(uuid) | 事件 ID,用于幂等去重。 |
| `task_id` | string | 任务 ID。 |
| `status` | string | `done` 或 `failed`(终态)。 |
| `event_type` | string | `contract.compare.completed`(成功)或 `contract.compare.failed`(失败)。 |
| `document_no` | string | 提交时的单据号。 |
| `change_status` | string | 仅 `done` 有。内容变化结论,同查询端点。 |
| `result_text` | string | 仅 `done` 有。中文结论文本,同查询端点。 |
| `highlight_images` | string[] | 仅 `done` 有。高亮图 URL 数组,同查询端点。 |
| `result_url` | string | 仅 `done` 有。PDF 报告下载绝对 URL,同查询端点。 |
| `html_url` | string | 仅 `done` 有。HTML 报告下载绝对 URL,同查询端点。 |
| `error` | string | 仅 `failed` 有。失败原因。 |

### 投递与重试机制

- **成功判定**:HTTP 响应状态码 `< 300`(即任意 2xx)视为投递成功。
- **失败重试**:最多重试 **3 次**,指数退避间隔 **1s → 4s → 16s**,单次请求超时 **10s**。
- **不重试的情况**:响应 2xx 即不再重试(即使你的业务逻辑处理失败)。因此若你的下游处理可能出错,请先响应 2xx 再异步处理,或依赖 `task_id` 幂等。

### 接收方建议

1. **响应 2xx 要快**:收到后立即回 `200`,把耗时处理放到后台,避免 webhook 超时被误判失败而重复投递。
2. **按 `event_id` 幂等**:同一事件可能投递多次(网络抖动 / 重试),务必用 `event_id` 去重。
3. **回调与查询互为补偿**:回调没到 → 用 `task_id` 轮询查询端点兜底。

---

## 十、枚举值速查

### `status`(任务状态)

| 值 | 含义 | 是否终态 |
|---|---|---|
| `pending` | 已受理,排队中 | 否 |
| `running` | 执行中 | 否 |
| `done` | 成功完成 | ✅ 是 |
| `failed` | 执行失败 | ✅ 是 |

### `change_status`(核心结论,仅 `done` 时)

| 值 | 中文 | 含义 |
|---|---|---|
| `changed` | 发现确认内容变化 | 存在确认的内容篡改(字符级 / 表格级差异) |
| `needs_review` | 存在待人工复核内容 | 识别质量不足(如 OCR 低质量),需人工复核 |
| `clean` | 未发现内容变化 | 未发现内容篡改 |

### `result_text` 中其它状态枚举

| 字段 | 值 | 中文 |
|---|---|---|
| 识别状态 | `reliable` | 可靠 |
| | `needs_review` | 待人工复核 |
| 高亮定位 | `complete` | 完整 |
| | `partial` | 部分缺失 |
| | `missing` | 缺失 |
| 差异类型(每条差异) | `modified` | 修改 |
| | `added` | 新增 |
| | `deleted` | 删除 |
| | `identical` | 一致 |

---

## 十一、排障:`request_id` 与审计

每个外部请求都会带一个 `request_id`(12 位十六进制),出现在:

- 响应头 `X-Request-Id`
- 错误响应体 `request_id` 字段

服务端会把每次入站调用(含成功、401 鉴权失败、422 校验失败、413、429 等)落库审计,记录端点、方法、单据号、来源 IP、Key 的 SHA-256 指纹、状态码、耗时等。

**如果调用出错或结果异常**,把 `request_id` 提供给服务管理员,即可在审计记录里定位到这一次完整调用链路,便于排查。这是它对调用方的主要价值——排障定位,非业务必需字段。

---

## 附:端点速查表

| 方法 | 路径 | 用途 | 鉴权 |
|---|---|---|---|
| POST | `/api/v1/external/contractCompare` | 提交比对(同步 / 异步) | `X-API-Key` |
| GET | `/api/v1/external/contractCompare/{task_id}` | 查询结果 / 状态 | `X-API-Key` |
| GET | `/api/v1/external/contractCompare/{task_id}/images/{page}` | 获取高亮 PNG | `X-API-Key` |
| GET | `/health` | 健康检查 | 无 |
