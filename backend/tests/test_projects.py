"""
项目相关接口功能测试
"""

import pytest


@pytest.mark.asyncio
async def test_health(async_client):
    """测试健康检查接口。"""
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_create_and_get_project(async_client):
    """测试创建与查询项目。"""
    response = await async_client.post("/api/projects", json={"name": "测试项目"})
    assert response.status_code == 200
    data = response.json()
    assert "id" in data
    project_id = data["id"]

    response = await async_client.get(f"/api/projects/{project_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == project_id
    assert data["name"] == "测试项目"
    assert data["voice_status"] == "pending"
