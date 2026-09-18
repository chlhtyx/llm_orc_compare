# 最小 callback 接收服务:用于验证主服务(llm_ocr_compare)的 webhook 回调。
# 仅用 Python 标准库,无需 pip 安装 → 镜像极小。
FROM python:3.12-alpine

WORKDIR /app

COPY server.py ./

RUN mkdir -p /app/logs

ENV CALLBACK_LOG_FILE=/app/logs/callbacks.log \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

EXPOSE 9000

CMD ["python", "server.py", "0.0.0.0", "9000"]
