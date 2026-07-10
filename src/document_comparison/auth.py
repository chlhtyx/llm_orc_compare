"""API Key 认证(§14.1)。

FastAPI 依赖注入:所有对外端点要求 X-API-Key。
生产应替换为数据库/秘密管理的 Key 校验。
"""
from __future__ import annotations

from fastapi import Header, HTTPException


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing API key")
    from .config import settings

    if x_api_key not in settings.api_keys:
        raise HTTPException(status_code=403, detail="invalid API key")
    return x_api_key
