"""
文件存储服务功能测试
"""

import pytest

from app.services.file_store import (
    get_project_dir,
    get_project_file_path,
    get_public_url,
    save_upload_file,
)


@pytest.mark.asyncio
async def test_project_dir_creation():
    """测试项目目录自动创建。"""
    project_id = "test_project_dir"
    project_dir = get_project_dir(project_id)
    assert project_dir.exists()


@pytest.mark.asyncio
async def test_save_and_read_file():
    """测试文件保存与路径解析。"""
    project_id = "test_save_file"
    content = b"hello world"
    path = save_upload_file(project_id, "test.txt", content)
    assert path.exists()
    assert path.read_bytes() == content

    public_url = get_public_url(project_id, "test.txt")
    assert public_url == f"/uploads/{project_id}/test.txt"

    file_path = get_project_file_path(project_id, "test.txt")
    assert file_path == path
