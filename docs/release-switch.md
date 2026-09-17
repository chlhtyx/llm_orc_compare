# 发布切换（原地升级）

单一正式部署，发布 = 排空后换镜像 tag 重建同一容器。没有候选容器、没有内部路由切换；
外部 Nginx 固定指向宿主端口 `DC_PORT`（默认 3012），发布全程不改域名、鉴权、回调地址和调用方 URL。
新镜像通过固定样本回归和人工核验后，按本流程发布。

## 一、部署状态机

`release_deployments.mode` 三态（`src/document_comparison/db/releases.py`）：

| 模式 | 新的 API 写请求 | 领取存量任务 | 查询和下载 |
| --- | --- | --- | --- |
| SERVING | 允许 | 允许 | 允许 |
| DRAINING | 503 + Retry-After | 允许，完成已有队列 | 允许 |
| STOPPED | 503 + Retry-After | 禁止 | 允许，直到容器停止 |

- 同一 `DC_DEPLOYMENT_ID` 跨版本复用；`register()` 自动更新版本记录并保留当前模式。
  `SERVING` 下可直接换镜像重建；显式 `DRAINING` / `STOPPED` 不会被启动覆盖，维护结束需 `serve`。
- `DC_DEPLOYMENT_INITIAL_MODE` 只允许 `SERVING`，仅在首次注册时生效；重启沿用 PG 中的维护状态。
- 同一个 PG 中最多允许一个 SERVING/DRAINING 部署；误起第二个容器会在注册时被拒绝。
- STOPPED 是逻辑状态，不会自行关闭进程。登录、退出和 GET 查询不受维护限制；
  所有 `/api/` 下非 GET/HEAD/OPTIONS 写请求（auth 除外）均受屏障保护，新加写路由自动受保护。
  外部维护请求依旧记录 503 审计；为避免读大文件，参数快照为空。

`GET /health` 为存活检查。`GET /ready` 检查数据库、Alembic head、部署版本、上传/报告目录读写与
调度器，仅在 SERVING 且全部正常时返回 200；STOPPED 下返回 503 但 `prepared=true` 属预期。
不访问 OCR/模型、不执行收费推理。`GET /api/v1/deployment` 返回模式与全库排空计数，沿用控制台鉴权。

发布控制只通过本机/容器 CLI 执行（`python -m document_comparison.release`，
或 `bash scripts/release.sh ctl CONTAINER <子命令>`），不新增远程控制接口。
所有状态/屏障变更都在 PG 事务中串行化；DB 不可用时拒绝新写请求，避免接收无法登记的工作。

## 二、发布前检查

1. 新镜像已构建并推送（`DC_VERSION` 与镜像 tag 同源），记录镜像 digest、Git 提交、
   迁移 head 与回归评估结果。构建一次，上线与回滚都用已保存的镜像，不在发布时重新构建。
2. 已完成固定样本回归、人工准确率核验、全流程功能验收、失败/重试与历史报告兼容验证。
   异步外部接口验收使用测试回调接收器，不得使用正式回调地址。
3. 迁移审阅：新容器启动时自动执行 `alembic upgrade head`（`DC_DB_AUTO_MIGRATE=1` 默认开）。
   **降级兼容性**：迁移先行，若迁移包含不向后兼容的 schema 变更，回滚后旧镜像可能无法运行；
   此类发布前必须确认迁移可逆，或明确接受不可回滚窗口。破坏性迁移（删字段、删旧报告支持）
   推迟到回滚窗口结束后的独立发布。
4. 排空后、重建前完成数据库逻辑备份与上传/报告存储备份。

## 三、发布步骤

```bash
# 1. 暂停提交 → 等待排空 → 配置快照 → 停机（任一步失败即停，不强杀任务）
bash scripts/release.sh prepare llm-ocr-compare before-<新版本> 1800

# 2. 换镜像并重建（.env 中 DC_VERSION=<新版本>）
docker compose pull llm-ocr-compare
docker compose up -d llm-ocr-compare

# 3. 就绪自检（期望 exit 0 / prepared=true；此时 HTTP /ready 503 属正常——尚未 serve）
bash scripts/release.sh ctl llm-ocr-compare ready

# 4. 接单
bash scripts/release.sh ctl llm-ocr-compare serve

# 5. 验收：统一入口 /ready 200 且版本正确；提交一条合成比对任务走通状态/报告/测试回调
curl -fsS http://127.0.0.1:${DC_PORT:-3012}/ready
```

说明：

- 排空判定（`release status` 的 `drained`）= pending（含无 queue_payload 的孤儿记录）、running、
  pending callback、`release_activities` 全为 0。请求在读取上传前登记，直至响应与该请求的异步审计
  收尾后才移除；任务在领取事务中登记，直到分析、持久化和后台回调结束才移除，`done` 不代表 job
  屏障已结束。已穷尽重试且持久化为 failed 的回调可通过排空，但发布记录需保留后续重推安排。
- `prepare` 输出的快照路径和 SHA256 记入发布记录。快照位于 `release-snapshots/`，权限 0600，
  含真实模型密钥与启动配置，不得提交 Git、写入普通日志或对外发布；目录只向运维授权。
  发布期间如需修改模型配置，应提前审核并保存配置快照。
- 从 stop 到 serve 之间（约 1-3 分钟：排空尾巴 + 容器重建 + 启动迁移）写请求 503 + `Retry-After`。
  调用方对响应丢失后的重试需核对单据/任务，不能把文件哈希视作幂等键。
- 迁移失败时新容器启动即退出（fail fast），不进入半服务状态；按回滚流程处理。
- 超时处理：`wait` 超时后保持 `DRAINING`，查看 `status` 计数，确认后可重新 `wait`，或
  `ctl serve` 取消本次发布恢复接单（保留已接收任务，不会伪造完成）。

## 四、回滚

**serve 之前发现问题**（未接单，无业务写入）：

```bash
# .env 中 DC_VERSION 改回旧版本
docker compose up -d llm-ocr-compare
bash scripts/release.sh ctl llm-ocr-compare serve
```

**serve 之后发现问题**（已接单，需再次排空）：

```bash
bash scripts/release.sh prepare llm-ocr-compare rollback-<新版本> 1800
# .env 中 DC_VERSION 改回旧版本；若新版本改过 llm_config 语义，先恢复快照：
# bash scripts/release.sh ctl llm-ocr-compare restore --file <prepare 输出路径> \
#   --sha256 <记录的 SHA256> --source-deployment <部署 ID>
docker compose up -d llm-ocr-compare
bash scripts/release.sh ctl llm-ocr-compare serve
```

`restore` 校验文件摘要与来源部署，仅在所有部署停止接单且全库排空时允许写入；它精确恢复
`llm_config`，不恢复数据库内容、不改运行环境变量。严禁把上线前的 DB 备份直接覆盖正式库作为普通
回滚，否则会丢失上线后新任务和审计。回滚后 `release status` 的 `version` 应显示旧版本。
若分析结果异常导致无法正常排空，停止自动流程：隔离受影响任务、核对文件与回调、确认执行进程停止
后再处理队列。进程故障后租约可能重新执行任务，不提供 exactly-once 回调保障。

## 五、异常屏障处理

进程取消、强杀或业务写库失败时，`release_activities` 保留记录且永不过期——这是"完成状态未知"，
不是可自动超时放行的锁泄漏。先暂停接单，定位 `status` 中的 owner/detail，确认该 owner 进程及其
OCR/文件写入线程已停止，核对任务状态、产物、回调和遗漏审计后，再移除对应屏障：

```bash
bash scripts/release.sh ctl llm-ocr-compare resolve-activity \
  --activity-id ACTIVITY_ID --confirmed-stopped-owner OWNER_ID
```

命令要求部署 ID、操作 ID、owner 精确匹配，且不能在 SERVING 下执行；它不修改任务或回调状态。
若仍有 pending/running/pending callback，切换继续被阻止；确认未知任务不能靠删除记录规避。

Gunicorn 优雅退出有 120 秒上限（Compose 等待 130 秒），它们只是兜底，不能替代 drain/wait。

## 六、特殊升级场景

- **从旧候选机制（STANDBY 四态）升级**：库里可能遗留 `STANDBY` 状态。用 one-off 容器归一后再发布：
  `docker compose run --rm llm-ocr-compare python -m document_comparison.release stop`。
  同时删除已废弃的 `data/release-router/` 目录与 `scripts/switch-release-router.sh` 引用。
- **从更早的不支持发布控制的版本升级**：旧代码不读取发布状态，无法用这套命令阻止其领取任务。
  首次升级需在网关暂停写请求，确认旧任务和回调完成，停止全部旧 worker，备份后
  `alembic upgrade head`，再启动本版本；不能同时运行未接入屏障的旧 worker 和新的正式执行器。
- **旧镜像仍提示更换版本前必须 stop**：该限制已取消，需重新构建或拉取包含修复的镜像。
  新镜像自动更新同一部署 ID 的版本记录；主动设置的 DRAINING / STOPPED 仍保留。
