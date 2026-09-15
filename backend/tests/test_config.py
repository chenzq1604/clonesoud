"""
配置管理路由测试

覆盖：
1. GET 返回掩码：API Key 完整值永不下发；
2. PUT 持久化到 .env（保留未托管键）并即时生效；
3. 空字符串密钥保持不变；
4. 非法 URL 与换行注入被拒绝。

测试通过 monkeypatch 将 .env 重定向到临时文件，
绝不触碰真实 backend/.env；settings 的改动也由
monkeypatch 在用例结束时自动还原。
"""

import app.routers.config as config_router
from app.config import settings


def _patch_env_file(monkeypatch, tmp_path, initial: str):
    """将配置路由的 .env 路径重定向到临时文件并写入初始内容。"""
    fake_env = tmp_path / ".env"
    fake_env.write_text(initial, encoding="utf-8")
    monkeypatch.setattr(config_router, "ENV_FILE", fake_env)
    return fake_env


async def test_get_config_masks_api_key(async_client):
    """GET 只返回掩码与是否已配置，完整密钥不下发。"""
    r = await async_client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert "ark_api_key" not in data  # 明文字段不存在
    assert "ark_api_key_masked" in data
    assert "ark_api_key_set" in data
    # 掩码不含密钥主体（只可能含末 6 位）
    masked = data["ark_api_key_masked"]
    assert settings.ark_api_key.endswith(masked.replace("****", "")) or masked == ""


async def test_update_config_persists_and_applies(async_client, tmp_path, monkeypatch):
    """PUT 更新持久化到 .env（保留其他键）且运行时即时生效。"""
    fake_env = _patch_env_file(
        monkeypatch, tmp_path, "ARK_API_KEY=old-secret\nCOMFYUI_TIMEOUT=1800\n"
    )
    # 记录原值，测试结束由 monkeypatch 还原（路由会直接 setattr）
    monkeypatch.setattr(settings, "comfyui_timeout", settings.comfyui_timeout)
    monkeypatch.setattr(settings, "demo_mode", settings.demo_mode)

    r = await async_client.put(
        "/api/config", json={"comfyui_timeout": 600, "demo_mode": True}
    )
    assert r.status_code == 200
    assert sorted(r.json()["updated"]) == ["comfyui_timeout", "demo_mode"]

    # .env 持久化：新值写入、未托管键保留
    text = fake_env.read_text(encoding="utf-8")
    assert "COMFYUI_TIMEOUT=600" in text
    assert "DEMO_MODE=true" in text
    assert "ARK_API_KEY=old-secret" in text

    # 运行时即时生效
    assert settings.comfyui_timeout == 600
    assert settings.demo_mode is True


async def test_update_config_empty_key_keeps_current(async_client, tmp_path, monkeypatch):
    """空字符串密钥表示保持不变，不覆盖已配置的 Key。"""
    fake_env = _patch_env_file(monkeypatch, tmp_path, "ARK_API_KEY=secret-keep\n")
    original_key = settings.ark_api_key
    monkeypatch.setattr(settings, "ark_api_key", original_key)
    monkeypatch.setattr(settings, "image_model", settings.image_model)

    r = await async_client.put(
        "/api/config", json={"ark_api_key": "", "image_model": "model-x"}
    )
    assert r.status_code == 200
    assert "ark_api_key" not in r.json()["updated"]  # 密钥未变更

    assert settings.ark_api_key == original_key  # 运行时保持原值
    assert "ARK_API_KEY=secret-keep" in fake_env.read_text(encoding="utf-8")  # 文件未被改写
    assert settings.image_model == "model-x"  # 其他字段正常更新


async def test_update_config_rejects_invalid_values(async_client, tmp_path, monkeypatch):
    """非法 URL 与含换行的值被拒绝，不产生任何写入。"""
    fake_env = _patch_env_file(monkeypatch, tmp_path, "ARK_API_KEY=k\n")

    # 非 http(s) 协议
    r = await async_client.put("/api/config", json={"comfyui_base_url": "ftp://127.0.0.1"})
    assert r.status_code == 400

    # 换行注入（试图写入额外的 .env 键）
    r = await async_client.put(
        "/api/config", json={"image_model": "m\nEVIL=injected"}
    )
    assert r.status_code == 400

    # 超时越界（Pydantic 层校验）
    r = await async_client.put("/api/config", json={"comfyui_timeout": 1})
    assert r.status_code == 422

    # 均未写入
    assert fake_env.read_text(encoding="utf-8") == "ARK_API_KEY=k\n"
