#!/usr/bin/env bash
# 在已经运行的容器内执行发布控制。不会猜测/修改网关，也不会强杀在途任务。
set -euo pipefail

usage() {
  cat <<'EOF'
用法：
  bash scripts/release.sh prepare CONTAINER LABEL [TIMEOUT_SECONDS]
  bash scripts/release.sh ctl CONTAINER COMMAND [ARGS...]

prepare：暂停提交 → 等待排空 → 保存私有配置快照 → 停机（STOPPED）。
失败立即停止，保留当前状态；超时后可用 ctl CONTAINER serve 取消排空。
快照路径和 SHA256 输出到终端，配置正文仅写容器内持久化目录。

原地发布后续步骤（换镜像、验收、回滚）见 docs/release-switch.md：
  .env 调整 DC_VERSION 后 docker compose pull && docker compose up -d llm-ocr-compare
  bash scripts/release.sh ctl llm-ocr-compare ready
  bash scripts/release.sh ctl llm-ocr-compare serve

示例：
  bash scripts/release.sh prepare llm-ocr-compare before-v2 1800
  bash scripts/release.sh ctl llm-ocr-compare status
  bash scripts/release.sh ctl llm-ocr-compare serve
EOF
}

if [[ $# -eq 0 || ${1:-} == --help ]]; then
  usage
  exit 0
fi

action=$1
shift
case "$action" in
  prepare)
    [[ $# -ge 2 && $# -le 3 ]] || { usage >&2; exit 2; }
    container=$1
    label=$2
    timeout_seconds=${3:-1800}
    [[ $timeout_seconds =~ ^[0-9]+$ ]] || { echo 'TIMEOUT_SECONDS 必须为非负整数' >&2; exit 2; }
    docker exec "$container" python -m document_comparison.release drain
    docker exec "$container" python -m document_comparison.release wait --timeout "$timeout_seconds"
    docker exec "$container" python -m document_comparison.release snapshot --label "$label"
    docker exec "$container" python -m document_comparison.release stop
    ;;
  ctl)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    container=$1
    shift
    docker exec "$container" python -m document_comparison.release "$@"
    ;;
  *) usage >&2; exit 2 ;;
esac
