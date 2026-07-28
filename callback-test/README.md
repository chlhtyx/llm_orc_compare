# callback-test

最小 callback 接收服务,用于验证主服务(`llm_orc_compare`)的 webhook 回调。

收到主服务 POST 过来的回调报文后,**把请求行、来源 IP、`X-Event-Id` 头、JSON body
原样写入日志**,并回 `200` 让主服务判定交付成功(主服务 `webhook.deliver` 把 status `< 300`
视为成功)。仅用 Python 标准库,无需 `pip install`。

## 主服务会发什么(对照)

- 方法:`POST`
- 头:`Content-Type: application/json`、`X-Event-Id: <uuid>`
- body 示例(平铺 envelope):

```json
{
  "event_id": "uuid",
  "task_id": "...",
  "status": "done",
  "event_type": "contract.compare.completed",
  "document_no": "...",
  "change_status": "changed",
  "result_text": "...",
  "highlight_images": ["http://..."],
  "result_url": "http://..."
}
```

## 启动

```bash
cd callback-test
docker compose up -d --build
```

验证服务本身:

```bash
curl http://localhost:9099/
# {"alive": true, "service": "callback-test"}
```

## 看收到的报文

```bash
# 二选一
docker logs -f callback-test
tail -f callback-test/logs/callbacks.log
```

每条记录形如:

```
======================================================================
[2026-07-27 15:00:00+0800] POST /
from      : 172.18.0.3:51234
X-Event-Id: 5b7c...
body (412 bytes):
{
  "event_id": "5b7c...",
  "task_id": "...",
  "status": "done",
  ...
}
======================================================================
```

## 把它接到主服务的 `callback_url`

主服务的 `callback_url` 由提交任务时传入(`/api/v1/external/contractCompare` 异步模式必填;
`/api/v1/compare` 可选)。根据主服务的运行方式选 URL:

**主服务也跑在 Docker(本机 Mac/Windows Docker Desktop):**

```bash
docker network connect llm_orc_compare_default callback-test
# 然后 callback_url 用容器名:
#   http://callback-test:9000/
```

**主服务跑在 Docker(Linux,无 host.docker.internal):**

```bash
# 用宿主机 IP,例如:
#   http://<宿主机 IP>:9099/
```

**主服务直接跑在宿主机(本地 uvicorn 开发):**

```
http://localhost:9099/
```

> 网络名 `llm_orc_compare_default` 来自 compose 项目名(默认=目录名)。
> 若改过项目名,用 `docker network ls | grep default` 找实际网络名。

## 手动触发一次回调验证

对已完成/失败(`done`/`failed`)且带 `callback_url` 的任务,可调用主服务的重投端点
快速验证,无需重跑比对:

```bash
curl -X POST 'http://localhost:3012/api/v1/tasks/<task_id>/redeliver-callback'
```

随后在 `docker logs -f callback-test` 中即可看到刚发过来的报文。

## 停止

```bash
docker compose down
```
