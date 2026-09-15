"""
本地文件存储与通用下载工具

每个项目拥有独立目录，所有产物本地化保存，避免火山临时 URL 过期。
"""

import base64
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.config import settings


def get_project_dir(project_id: str) -> Path:
    """获取指定项目的本地目录，不存在则创建。"""
    project_dir = settings.upload_root / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


def get_project_file_path(project_id: str, filename: str) -> Path:
    """获取项目内某个文件的绝对路径。"""
    return get_project_dir(project_id) / filename


def save_upload_file(project_id: str, filename: str, data: bytes) -> Path:
    """将上传字节保存到项目目录。"""
    target = get_project_file_path(project_id, filename)
    target.write_bytes(data)
    return target


def read_file_base64(path: Path) -> str:
    """读取文件并返回 base64 字符串。"""
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def remove_project_dir(project_id: str) -> None:
    """删除整个项目目录。"""
    project_dir = settings.upload_root / project_id
    if project_dir.exists():
        shutil.rmtree(project_dir)


def get_public_url(project_id: str, filename: str) -> str:
    """根据配置生成产物对外访问 URL。"""
    return f"/uploads/{project_id}/{filename}"


async def download_url_to_file(
    url: str,
    target_path: Path,
    timeout: float = 120.0,
) -> Path:
    """
    通过 httpx 异步下载远程文件到本地。

    后端调用火山接口时已经配置代理，下载通常可直接复用该代理。
    """
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        target_path.write_bytes(response.content)
    return target_path


def safe_filename(filename: str) -> str:
    """移除文件名中的危险字符，避免路径穿越。"""
    return Path(filename).name


def infer_extension_from_url(url: str, default: str = ".bin") -> str:
    """从 URL 路径推断文件扩展名。"""
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix
    return suffix if suffix else default
