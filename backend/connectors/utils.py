import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from backend.config import CACHE_DIR, settings


def compute_sha256(text: str) -> str:
    """计算规范化文本的 SHA-256 指纹，用于严格去重"""
    normalized = " ".join(text.strip().split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def get_http_client() -> httpx.Client:
    """创建带标准请求头和超时控制的 HTTP 客户端"""
    headers = {
        "User-Agent": f"HisTagKGBuilder/1.0 (mailto:{settings.OPENALEX_EMAIL})",
        "Accept": "application/json, text/html, application/pdf, */*",
    }
    return httpx.Client(
        headers=headers,
        timeout=settings.REQUEST_TIMEOUT,
        follow_redirects=True,
    )


def save_raw_snapshot(category: str, filename: str, content: Any) -> Path:
    """将真实获取的原始数据保存到本地 raw_cache 目录下供审计"""
    target_dir = CACHE_DIR / category
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / filename

    if isinstance(content, bytes):
        file_path.write_bytes(content)
    elif isinstance(content, (dict, list)):
        file_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        file_path.write_text(str(content), encoding="utf-8")
    return file_path