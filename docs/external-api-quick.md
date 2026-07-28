# 合同比对 API 



## 通用

```
Base  {external_public_base_url}/api/v1/external
鉴权  X-API-Key: <KEY>           # 服务端未配 Key 时可省略
格式  提交用 multipart/form-data
```

**错误统一返回** `{"code", "message", "request_id"}`;成功响应无统一外壳。

---

## 提交比对

`POST /api/v1/external/contractCompare`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source` | file | ✅ | 原始合同,**后缀 `.docx`** |
| `target` | file | ✅ | 回收件,**后缀 `.pdf`** |
| `document_no` | string | ✅ | 单据号,非空,≤ 255 字符 |
| `sync` | bool | ❌ | 默认 `false`。`true`=同步,`false`=异步 |
| `callback_url` | string | 异步必填 | http(s) 回调地址,禁止带账号密码 |
| `original_page_count` | int | ❌ | ≥ 1;仅服务端开启「页数截取」时生效 |

**同步模式** `sync=true`:HTTP 一直阻塞到完成,响应体直接返回完整结果(状态码恒为 200,成败看 body 的 `status`)。客户端超时建议 ≥ 5 分钟。

**异步模式** `sync=false`:立即返回 202:

```json
{ "task_id": "...", "document_no": "...", "status": "pending" }
```

结果经回调推送,或用 `task_id` 查询。

### curl 示例

```bash
# 异步
curl -X POST {BASE}/contractCompare \
  -H "X-API-Key: KEY" \
  -F "source=@原.docx" -F "target=@回.pdf" \
  -F "document_no=HT-001" \
  -F "callback_url=https://your/hooks/compare"

# 同步
curl -X POST {BASE}/contractCompare \
  -H "X-API-Key: KEY" \
  -F "source=@原.docx" -F "target=@回.pdf" \
  -F "document_no=HT-001" -F "sync=true"
```

---

## 查询结果

`GET /api/v1/external/contractCompare/{task_id}` → 200

```json
{
  "task_id": "...",
  "document_no": "HT-001",
  "status": "done",          // pending | running | done | failed
  "stage": "done",
  "progress": 1.0,
  "error": null,
  // ↓ 仅 status=="done" 才有
  "change_status": "changed", // changed | needs_review | clean
  "result_text": "...",
  "highlight_images": [".../images/1", ".../images/2"],
  "result_url": "..."
}
```

`task_id` 不存在 → 404。

---

## 取高亮图

`GET /api/v1/external/contractCompare/{task_id}/images/{page}` → 200 `image/png`

- `page` **从 1 开始**。
- 越界 / 未生成 / task 不存在 → 404。
- 仅 `done` 后才有图。

---

## 任务状态机

```
pending → running → done
                  → failed
```

`done` / `failed` 为终态。无 `cancelled`。

---

## 错误码

| HTTP | 含义 | 是否重试 |
|---|---|---|
| 400 | 参数 / 文件类型 / URL 校验失败 | ❌ |
| 401 | `X-API-Key` 缺失或不匹配 | ❌ |
| 404 | task / 图片不存在 | ❌ |
| 413 | 单文件超上限(默认 50 MiB) | ❌ |
| 422 | form 字段缺失或类型错误 | ❌ |
| 429 | 并发已满 | ✅ 退避重试 |
| 5xx / 503 | 服务端故障或未配置 | ✅ |

---

## 回调(异步)

任务到终态时 POST 你的 `callback_url`,头带 `X-Event-Id`(幂等去重用)。

```json
// 成功
{ "event_id":"...", "task_id":"...", "status":"done",
  "event_type":"contract.compare.completed",
  "document_no":"...", "change_status":"changed",
  "result_text":"...", "highlight_images":[], "result_url":"..." }

// 失败
{ "event_id":"...", "task_id":"...", "status":"failed",
  "event_type":"contract.compare.failed",
  "document_no":"...", "error":"..." }
```

2xx 视为成功;失败重试 3 次,间隔 1s → 4s → 16s。建议收到立即回 200,按 `event_id` 去重。

---

## 关键结论枚举 `change_status`

| 值 | 含义 |
|---|---|
| `changed` | 发现确认内容变化 |
| `needs_review` | 存在待人工复核内容 |
| `clean` | 未发现内容变化 |
