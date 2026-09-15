"""
TTS 语速选择与语音库功能测试

覆盖：
1. 语速参数校验：合法档位（0.5/0.7/1.0/1.2/1.25/1.5/2.0）通过、越界与非数值 422、默认 1.0；
2. 语速处理：推理阶段恒定 1.0x（speed 不透传给 CosyVoice，避免 mel 插值
   变速破坏音色），变速由转码阶段 atempo 完成，产物时长按 1/speed 缩放；
3. 生成自动入库：元信息完整（文本/语速/友好音色名/来源项目）、
   音频文件落盘、试听地址可访问；
4. 语音库列表：按生成时间倒序、文件缺失时 url 为 null；
5. 选用：复制为项目 tts.mp3 并置 TTS 就绪、不改变音色绑定、
   条目/项目不存在与文件缺失时 404；
6. 删除：条目与文件清理、项目内副本不受影响、重复删除 404；
7. 合成失败时不产生语音库条目。

说明：单元测试不依赖本地 CosyVoice GPU 服务，推理调用以 mock 替代。
"""

import asyncio
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import settings
from app.services import tts as tts_service
from app.services.tts import get_tts_library_dir

UPLOAD_ROOT = settings.upload_root

# 模拟合成产物的固定时长（秒），供 atempo 时长缩放断言使用；
# 取 3s 使 mp3 封装的固定编码器开销（约 0.06s）相对可忽略
MOCK_WAV_DURATION = 3.0


def _probe_duration(path: Path) -> float:
    """用 ffprobe 读取音频文件时长（秒）。"""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    )
    return float(result.stdout.strip())


@pytest.fixture(autouse=True)
def mock_cosyvoice(monkeypatch):
    """
    模拟 CosyVoice 推理服务（合成调用与内置音色查询）。

    返回列表记录每次合成调用收到的额外关键字参数（kwargs），
    供"speed 不透传给推理服务"断言使用；
    每个用例开始前清空测试语音库目录，避免跨用例残留文件。
    """
    extra_kwargs: list[dict] = []

    async def fake_call(text, speaker_id, ref_audio_path, output_path, **kwargs):
        """模拟合成：生成一段短正弦波 WAV，并记录额外调用参数。"""
        extra_kwargs.append(kwargs)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "sine=frequency=440:r=24000",
                "-t", str(MOCK_WAV_DURATION), "-ac", "1", "-ar", "24000",
                "-acodec", "pcm_s16le", str(output_path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
        return {"output_path": str(output_path), "sample_rate": 24000, "duration": MOCK_WAV_DURATION}

    async def fake_speakers():
        """模拟内置音色列表。"""
        return [{"id": "xiaoxiao", "name": "晓晓（女声·温柔）"}]

    monkeypatch.setattr(tts_service, "_call_cosyvoice_tts", fake_call)
    monkeypatch.setattr(tts_service, "list_builtin_speakers", fake_speakers)

    # 清空测试语音库目录（跟随 conftest 的 uploads_test 隔离路径）
    library_dir = get_tts_library_dir()
    if library_dir.exists():
        shutil.rmtree(library_dir)
    library_dir.mkdir(parents=True, exist_ok=True)

    return extra_kwargs


async def _create_project(async_client, name: str) -> str:
    """创建项目并返回 ID。"""
    response = await async_client.post("/api/projects", json={"name": name})
    assert response.status_code == 200
    return response.json()["id"]


async def _generate(async_client, project_id: str, text: str, speed: float | None = None):
    """以内置音色发起 TTS 生成，可选指定语速。"""
    body = {"project_id": project_id, "text": text, "speaker_id": "builtin:xiaoxiao"}
    if speed is not None:
        body["speed"] = speed
    return await async_client.post("/api/tts/generate", json=body)


# ========== 语速选择 ==========


@pytest.mark.asyncio
@pytest.mark.parametrize("speed", [0.5, 0.7, 1.0, 1.2, 1.25, 1.5, 2.0])
async def test_speed_valid_options(async_client, mock_cosyvoice, speed):
    """七个合法语速档位均应合成成功、入库，且产物时长按 1/speed 缩放。"""
    project_id = await _create_project(async_client, "语速档位测试")
    response = await _generate(async_client, project_id, "语速档位测试文本", speed=speed)

    assert response.status_code == 200, response.text
    # 推理阶段恒定 1.0x：speed 不透传给 CosyVoice（避免 mel 插值变速破坏音色）
    assert mock_cosyvoice[-1] == {}

    items = (await async_client.get("/api/tts/library")).json()
    assert len(items) == 1
    assert items[0]["speed"] == speed

    # 变速由 atempo 完成：产物时长应约等于 mock 时长 / speed
    # （mp3 封装有约 0.06s 固定编码器开销，容差 0.1s 吸收）
    mp3_path = get_tts_library_dir() / Path(items[0]["url"]).name
    duration = _probe_duration(mp3_path)
    assert duration == pytest.approx(MOCK_WAV_DURATION / speed, abs=0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("speed", [-1.0, 0.0, 0.4, 2.1, 100.0])
async def test_speed_out_of_range_rejected(async_client, mock_cosyvoice, speed):
    """超出 [0.5, 2.0] 的语速应返回 422，且不触发合成与入库。"""
    project_id = await _create_project(async_client, "语速越界测试")
    response = await _generate(async_client, project_id, "语速越界测试文本", speed=speed)

    assert response.status_code == 422
    assert mock_cosyvoice == []
    assert (await async_client.get("/api/tts/library")).json() == []


@pytest.mark.asyncio
async def test_speed_invalid_type_rejected(async_client):
    """非数值语速应返回 422。"""
    project_id = await _create_project(async_client, "语速类型测试")
    response = await async_client.post(
        "/api/tts/generate",
        json={
            "project_id": project_id,
            "text": "语速类型测试文本",
            "speaker_id": "builtin:xiaoxiao",
            "speed": "fast",
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_speed_defaults_to_one(async_client, mock_cosyvoice):
    """未指定语速时应默认 1.0（正常语速，产物不做变速）。"""
    project_id = await _create_project(async_client, "默认语速测试")
    response = await _generate(async_client, project_id, "默认语速测试文本")

    assert response.status_code == 200
    assert mock_cosyvoice[-1] == {}

    items = (await async_client.get("/api/tts/library")).json()
    assert items[0]["speed"] == 1.0
    # 1.0x 不做变速：产物时长与合成时长一致（含 mp3 固定开销容差）
    mp3_path = get_tts_library_dir() / Path(items[0]["url"]).name
    assert _probe_duration(mp3_path) == pytest.approx(MOCK_WAV_DURATION, abs=0.1)


# ========== 语音库：生成入库与列表 ==========


@pytest.mark.asyncio
async def test_generate_saves_to_library(async_client):
    """生成后应自动入库：元信息完整、文件落盘、试听地址可访问。"""
    project_id = await _create_project(async_client, "入库测试")
    response = await _generate(async_client, project_id, "这段语音应自动入库", speed=1.5)
    assert response.status_code == 200

    items = (await async_client.get("/api/tts/library")).json()
    assert len(items) == 1
    item = items[0]
    assert item["project_id"] == project_id
    assert item["text"] == "这段语音应自动入库"
    assert item["speed"] == 1.5
    assert item["speaker_id"] == "builtin:xiaoxiao"
    # 内置音色应保存清单中的友好显示名（而非裸 ID）
    assert item["speaker_name"] == "晓晓（女声·温柔）"
    assert item["created_at"] is not None
    # 时间须带 UTC 时区标记，前端才能正确换算为北京时间（东八区）
    assert item["created_at"].endswith("+00:00")

    # 音频文件真实存在，且试听地址可访问
    assert item["url"].startswith("/uploads/_tts_library/")
    assert item["url"].endswith(".mp3")
    assert (get_tts_library_dir() / Path(item["url"]).name).exists()
    audio = await async_client.get(item["url"])
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/")


@pytest.mark.asyncio
async def test_created_at_is_utc_with_timezone(async_client):
    """
    项目与语音库的时间字段应带 UTC 时区标记。

    数据库以无时区的 utcnow() 存储，若序列化后缺少时区标记，
    前端 new Date() 会当作本地时间解析，导致北京时间显示早 8 小时。
    """
    project_id = await _create_project(async_client, "时区标记测试")
    await _generate(async_client, project_id, "时区标记测试语音")

    item = (await async_client.get("/api/tts/library")).json()[0]
    created = datetime.fromisoformat(item["created_at"])
    assert created.tzinfo is not None
    assert created.utcoffset() == timedelta(0)
    # 应为当前时刻附近（存的是 UTC，序列化后仍表示同一时刻）
    assert abs((datetime.now(timezone.utc) - created).total_seconds()) < 60

    project = (await async_client.get(f"/api/projects/{project_id}")).json()
    for field in ("created_at", "updated_at"):
        parsed = datetime.fromisoformat(project[field])
        assert parsed.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_library_list_ordered_desc(async_client):
    """语音库应按生成时间倒序排列（最新在前）。"""
    project_id = await _create_project(async_client, "排序测试")
    await _generate(async_client, project_id, "第一条语音")
    # created_at 为秒级精度，间隔一秒以上保证时间可区分
    await asyncio.sleep(1.1)
    await _generate(async_client, project_id, "第二条语音")

    items = (await async_client.get("/api/tts/library")).json()
    assert len(items) == 2
    assert items[0]["text"] == "第二条语音"
    assert items[1]["text"] == "第一条语音"


@pytest.mark.asyncio
async def test_library_url_none_when_file_missing(async_client):
    """条目音频文件被手动删除后，列表 url 应为 null（记录保留）。"""
    project_id = await _create_project(async_client, "文件缺失列表测试")
    await _generate(async_client, project_id, "将被删文件的语音")

    items = (await async_client.get("/api/tts/library")).json()
    (get_tts_library_dir() / Path(items[0]["url"]).name).unlink()

    items = (await async_client.get("/api/tts/library")).json()
    assert len(items) == 1
    assert items[0]["url"] is None


@pytest.mark.asyncio
async def test_generate_failure_not_saved_to_library(async_client, monkeypatch):
    """合成失败时应返回 500、项目标记失败，且不产生语音库条目。"""

    async def failing_call(*args, **kwargs):
        """模拟推理服务失败。"""
        raise RuntimeError("模拟推理失败")

    monkeypatch.setattr(tts_service, "_call_cosyvoice_tts", failing_call)

    project_id = await _create_project(async_client, "失败入库测试")
    response = await _generate(async_client, project_id, "会失败的语音")
    assert response.status_code == 500
    assert (await async_client.get("/api/tts/library")).json() == []

    project = (await async_client.get(f"/api/projects/{project_id}")).json()
    assert project["tts_status"] == "failed"


@pytest.mark.asyncio
async def test_error_cleared_after_successful_retry(async_client, monkeypatch):
    """失败后重试成功时，应清空上一次残留的错误详情。"""

    async def failing_call(*args, **kwargs):
        """模拟首次合成失败。"""
        raise RuntimeError("模拟首次失败")

    monkeypatch.setattr(tts_service, "_call_cosyvoice_tts", failing_call)

    project_id = await _create_project(async_client, "失败后重试测试")
    assert (await _generate(async_client, project_id, "首次失败")).status_code == 500
    project = (await async_client.get(f"/api/projects/{project_id}")).json()
    assert project["tts_status"] == "failed"
    assert project["tts_error"]

    async def ok_call(text, speaker_id, ref_audio_path, output_path, **kwargs):
        """模拟重试成功。"""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "sine=frequency=440:r=24000",
                "-t", str(MOCK_WAV_DURATION), "-ac", "1", "-ar", "24000",
                "-acodec", "pcm_s16le", str(output_path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
        return {
            "output_path": str(output_path),
            "sample_rate": 24000,
            "duration": MOCK_WAV_DURATION,
        }

    monkeypatch.setattr(tts_service, "_call_cosyvoice_tts", ok_call)

    assert (await _generate(async_client, project_id, "重试成功")).status_code == 200
    project = (await async_client.get(f"/api/projects/{project_id}")).json()
    assert project["tts_status"] == "ready"
    assert project["tts_error"] is None


# ========== 语音库：选用 ==========


@pytest.mark.asyncio
async def test_use_library_item(async_client):
    """选用条目应复制为项目 tts.mp3 并置 TTS 就绪，且不改变音色绑定。"""
    source_id = await _create_project(async_client, "选用来源项目")
    await _generate(async_client, source_id, "待选用的语音", speed=0.7)
    item = (await async_client.get("/api/tts/library")).json()[0]

    target_id = await _create_project(async_client, "选用目标项目")
    response = await async_client.post(
        f"/api/tts/library/{item['id']}/use", json={"project_id": target_id}
    )
    assert response.status_code == 200, response.text
    assert response.json()["tts_path"] == f"/uploads/{target_id}/tts.mp3"

    project = (await async_client.get(f"/api/projects/{target_id}")).json()
    assert project["tts_status"] == "ready"
    assert project["tts_text"] == "待选用的语音"
    assert project["tts_path"] == f"/uploads/{target_id}/tts.mp3"
    # 选用只影响 TTS 产物，不改变音色绑定状态
    assert project["speaker_id"] is None
    assert project["voice_status"] == "pending"

    # 目标项目文件应与语音库源文件内容一致
    library_file = get_tts_library_dir() / Path(item["url"]).name
    target_file = UPLOAD_ROOT / target_id / "tts.mp3"
    assert target_file.exists()
    assert library_file.read_bytes() == target_file.read_bytes()


@pytest.mark.asyncio
async def test_use_nonexistent_item(async_client):
    """选用不存在的条目应返回 404。"""
    project_id = await _create_project(async_client, "选用404条目测试")
    response = await async_client.post(
        "/api/tts/library/nonexistent_id/use", json={"project_id": project_id}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_use_nonexistent_project(async_client):
    """向不存在的项目选用应返回 404。"""
    source_id = await _create_project(async_client, "选用404项目来源")
    await _generate(async_client, source_id, "语音")
    item = (await async_client.get("/api/tts/library")).json()[0]

    response = await async_client.post(
        f"/api/tts/library/{item['id']}/use", json={"project_id": "nonexistent_id"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_use_item_with_missing_file(async_client):
    """条目音频文件缺失时选用应返回 404 并提示文件缺失。"""
    source_id = await _create_project(async_client, "文件缺失选用测试")
    await _generate(async_client, source_id, "文件会被删除的语音")
    item = (await async_client.get("/api/tts/library")).json()[0]
    (get_tts_library_dir() / Path(item["url"]).name).unlink()

    response = await async_client.post(
        f"/api/tts/library/{item['id']}/use", json={"project_id": source_id}
    )
    assert response.status_code == 404
    assert "音频文件已缺失" in response.json()["detail"]


# ========== 语音库：删除 ==========


@pytest.mark.asyncio
async def test_delete_library_item(async_client):
    """删除条目应同时清理记录与音频文件，重复删除返回 404。"""
    project_id = await _create_project(async_client, "删除测试")
    await _generate(async_client, project_id, "将被删除的语音")

    items = (await async_client.get("/api/tts/library")).json()
    assert len(items) == 1
    item_id = items[0]["id"]
    file_path = get_tts_library_dir() / Path(items[0]["url"]).name
    assert file_path.exists()

    response = await async_client.delete(f"/api/tts/library/{item_id}")
    assert response.status_code == 200
    assert not file_path.exists()
    assert (await async_client.get("/api/tts/library")).json() == []

    response = await async_client.delete(f"/api/tts/library/{item_id}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_nonexistent_item(async_client):
    """删除不存在的条目应返回 404。"""
    await _create_project(async_client, "删除404测试")
    response = await async_client.delete("/api/tts/library/nonexistent_id")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_keeps_project_copy(async_client):
    """删除语音库条目不应影响已选用到项目中的 tts.mp3 副本。"""
    source_id = await _create_project(async_client, "删除副本保护来源")
    await _generate(async_client, source_id, "语音A")
    item = (await async_client.get("/api/tts/library")).json()[0]

    target_id = await _create_project(async_client, "删除副本保护目标")
    use_response = await async_client.post(
        f"/api/tts/library/{item['id']}/use", json={"project_id": target_id}
    )
    assert use_response.status_code == 200

    delete_response = await async_client.delete(f"/api/tts/library/{item['id']}")
    assert delete_response.status_code == 200

    # 目标项目的语音副本与就绪状态不受影响
    project = (await async_client.get(f"/api/projects/{target_id}")).json()
    assert project["tts_status"] == "ready"
    assert (UPLOAD_ROOT / target_id / "tts.mp3").exists()
