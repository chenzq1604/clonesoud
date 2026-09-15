"""
配置管理路由

提供运行参数的查询与修改（含火山引擎 API Key 等敏感项），
修改后同时更新运行时配置并持久化到 .env 文件，重启后仍然生效。

安全设计：
- API Key 查询时只返回掩码（**** + 末 6 位），完整值永不下发；
- 更新时传空字符串表示保持不变，避免前端为了改其他配置而回传明文；
- 本服务仅绑定 127.0.0.1 本地使用，请勿暴露到公网。
"""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.schemas import ConfigUpdate

router = APIRouter()

# 可通过配置页面编辑的字段：Settings 属性名 -> .env 键名
EDITABLE_FIELDS = {
    "ark_api_key": "ARK_API_KEY",
    "image_model": "IMAGE_MODEL",
    "http_proxy": "HTTP_PROXY",
    "comfyui_base_url": "COMFYUI_BASE_URL",
    "comfyui_timeout": "COMFYUI_TIMEOUT",
    "cosyvoice_base_url": "COSYVOICE_BASE_URL",
    "cosyvoice_timeout": "COSYVOICE_TIMEOUT",
    "demo_mode": "DEMO_MODE",
}

# .env 文件路径（app/config.py 的上两级即 backend 目录）
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _mask_secret(value: str) -> str:
    """将密钥掩码为 **** + 末 6 位（不足 6 位时全掩码）。"""
    if not value:
        return ""
    tail = value[-6:] if len(value) > 6 else ""
    return f"****{tail}"


def _serialize_env_value(field: str, value) -> str:
    """将配置值序列化为 .env 行内的字符串形式。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _validate_url_field(field: str, value: str) -> None:
    """校验 URL 类字段必须以 http:// 或 https:// 开头。"""
    if value and not re.match(r"^https?://", value):
        raise HTTPException(status_code=400, detail=f"{field} 必须以 http:// 或 https:// 开头")


def _persist_to_env_file(updates: dict[str, str]) -> None:
    """
    将更新写回 .env 文件（保留原有注释与未托管键）。

    逐行扫描：匹配到已托管键则原位替换；文件末尾追加缺失的键。
    写入失败（如文件被占用）抛出 RuntimeError，由调用方转 HTTP 500。
    """
    managed = set(updates)
    lines: list[str] = []
    seen: set[str] = set()
    if ENV_FILE.exists():
        text = ENV_FILE.read_text(encoding="utf-8")
        for line in text.splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if m and m.group(1) in managed:
                key = m.group(1)
                lines.append(f"{key}={updates[key]}")
                seen.add(key)
            else:
                lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    try:
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"写入 .env 失败: {exc}") from exc


@router.get("")
async def get_config():
    """查询当前配置（API Key 仅返回掩码，完整值永不下发）。"""
    return {
        "ark_api_key_masked": _mask_secret(settings.ark_api_key),
        "ark_api_key_set": bool(settings.ark_api_key),
        "image_model": settings.image_model,
        "http_proxy": settings.http_proxy,
        "comfyui_base_url": settings.comfyui_base_url,
        "comfyui_timeout": settings.comfyui_timeout,
        "cosyvoice_base_url": settings.cosyvoice_base_url,
        "cosyvoice_timeout": settings.cosyvoice_timeout,
        "demo_mode": settings.demo_mode,
    }


@router.put("")
async def update_config(data: ConfigUpdate):
    """
    更新配置：校验 -> 写 .env 持久化 -> 更新运行时对象。

    ark_api_key 传空字符串或不传表示保持不变；其余字段不传即不变。
    """
    # 收集实际变更（排除 None 与空字符串的密钥）
    changes: dict[str, object] = {}
    for field in EDITABLE_FIELDS:
        value = getattr(data, field)
        if value is None:
            continue
        if field == "ark_api_key" and value == "":
            continue  # 空字符串 = 保持当前密钥不变
        changes[field] = value

    if not changes:
        return {"message": "配置无变更", "updated": []}

    # 拒绝包含换行的值：防止写入 .env 时注入额外的环境变量行
    for field, value in changes.items():
        if isinstance(value, str) and ("\n" in value or "\r" in value):
            raise HTTPException(status_code=400, detail=f"{field} 不能包含换行符")

    # 校验 URL 类字段
    for field in ("http_proxy", "comfyui_base_url", "cosyvoice_base_url"):
        if field in changes:
            _validate_url_field(field, str(changes[field]))

    # 持久化到 .env（字段名映射为环境变量键名）
    env_updates = {EDITABLE_FIELDS[f]: _serialize_env_value(f, v) for f, v in changes.items()}
    _persist_to_env_file(env_updates)

    # 更新运行时配置对象（立即生效，无需重启）
    for field, value in changes.items():
        setattr(settings, field, value)

    return {"message": "配置已保存并即时生效", "updated": sorted(changes)}
