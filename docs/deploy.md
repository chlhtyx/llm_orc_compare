# Docker 部署指南

## 目标环境

Linux x86_64(amd64)、Docker 20.10+、Docker Compose v2。

## 快速开始

```bash
# 1. 进入项目目录
cd document_comparison

# 2. 准备环境变量
cp .env.example .env
# 编辑 .env,至少修改 DC_API_KEYS

# 3. 构建并启动
docker compose up -d --build

# 4. 检查状态
docker compose ps
curl http://localhost:8000/health
```

浏览器打开 `http://<服务器IP>:8000` 即可访问。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DC_PORT` | `8000` | 宿主机映射端口(容器内固定 8000) |
| `DC_API_KEYS` | `dev-key-please-change` | 认证 Key,逗号分隔多个;**生产务必替换** |
| `DC_OCR_BACKEND` | `mock` | OCR 引擎:`mock` / `vllm` / `paddle` / `llm` |
| `DC_LLM_API_BASE` | 阿里云 DashScope | LLM/VLM 推理地址(兼容 OpenAI 协议) |
| `DC_LLM_API_KEY` | (空) | LLM API Key |
| `DC_LLM_MODEL` | `qwen-vl-max` | 模型名 |
| `DC_LLM_TIMEOUT` | `120` | 单页识别超时(秒) |
| `DC_LLM_MAX_CONCURRENCY` | `4` | 并发页数 |
| `DC_EMBED_BACKEND` | `mock` | 向量引擎:`mock` / `bge` |

## 数据持久化

上传文件和比对报告存储在 Docker volume `dc_data`,映射到容器内 `/app/.dc_data`。

备份:

    docker run --rm -v document_comparison_dc_data:/data \
        -v $(pwd):/backup alpine \
        tar czf /backup/dc_data_backup.tar.gz -C /data .

恢复:

    docker run --rm -v document_comparison_dc_data:/data \
        -v $(pwd):/backup alpine \
        tar xzf /backup/dc_data_backup.tar.gz -C /data .

## 日常运维

    # 查看日志
    docker compose logs -f

    # 重启
    docker compose restart

    # 更新代码后重新部署
    docker compose up -d --build

    # 停止 / 删除
    docker compose down            # 仅停止,保留数据
    docker compose down -v         # 停止并删除数据卷(谨慎!)

## 使用真实 OCR 引擎

默认 `mock` 后端不调用任何外部服务。要启用真实 OCR:

1. 在 `.env` 中设置 `DC_OCR_BACKEND=vllm`(或 `llm` / `paddle`)
2. 配置 `DC_LLM_API_BASE`、`DC_LLM_API_KEY`、`DC_LLM_MODEL` 指向你的推理服务
3. 也可以在启动后通过 UI 的"设置"页面在线修改(会持久化到数据卷)

## 反向代理(Nginx)

如需 HTTPS 或自定义域名,在前面加 Nginx:

    server {
        listen 80;
        server_name your-domain.com;

        client_max_body_size 100m;

        location / {
            proxy_pass http://127.0.0.1:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
