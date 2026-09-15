"""
音色列表与 TTS 音色选择功能测试

覆盖：
1. 上传录音后 raw_recording.wav 为真实 WAV（RIFF 头）；
2. /api/voice/voices 返回内置音色 + 已克隆音色分组列表（含原音试听地址）；
3. TTS 可指定历史克隆音色 speaker_id，未克隆的新项目可直接使用；
   未显式传 speaker_id 时也能回溯到音色来源项目的录音；
4. TTS 可指定内置音色（builtin: 前缀）；
5. 非法 speaker_id 返回 400；
6. TTS 产物为可听音频（非静音）；
7. 遗留非法录音（.wav 实为 WebM）合成前自动转码修复；
8. 推理服务返回非 JSON 错误时抛出可读提示而非 JSONDecodeError。

说明：单元测试不依赖本地 CosyVoice GPU 服务，推理调用以 mock 替代。
"""

import math
import re
import struct
import subprocess
import wave
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# 上传根目录跟随应用配置（conftest 已将测试切换到独立的 uploads_test）
from app.config import settings
from app.services.tts import _call_cosyvoice_tts as real_call_cosyvoice_tts
from app.services.tts import get_builtin_preview_path

UPLOAD_ROOT = settings.upload_root


@pytest.fixture(autouse=True)
def mock_cosyvoice(monkeypatch):
    """模拟 CosyVoice 推理服务（合成调用与内置音色查询）。"""
    from app.services import tts as tts_service

    async def fake_call(text, speaker_id, ref_audio_path, output_path, speed=1.0):
        """模拟合成：用 FFmpeg 生成一段可听的提示音 WAV。"""
        duration = max(0.5, min(10.0, len(text) / 5.0))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "sine=frequency=440:r=24000",
                "-t", str(duration),
                "-ac", "1", "-ar", "24000",
                "-acodec", "pcm_s16le", str(output_path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
        )
        return {"output_path": str(output_path), "sample_rate": 24000, "duration": duration}

    async def fake_speakers():
        """模拟内置音色列表（id + 展示名）。"""
        return [
            {"id": "xiaoxiao", "name": "晓晓（女声·温柔）"},
            {"id": "yunxi", "name": "云希（男声·阳光）"},
        ]

    monkeypatch.setattr(tts_service, "_call_cosyvoice_tts", fake_call)
    monkeypatch.setattr(tts_service, "list_builtin_speakers", fake_speakers)


def _make_tone_wav(duration: float = 1.0, freq: float = 440.0) -> bytes:
    """用标准库生成一段 16kHz 单声道正弦波 WAV，模拟用户录音。"""
    import io

    buffer = io.BytesIO()
    sample_rate = 16000
    n_samples = int(duration * sample_rate)
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        for i in range(n_samples):
            value = int(20000 * math.sin(2 * math.pi * freq * i / sample_rate))
            wav_file.writeframes(struct.pack("<h", value))
    return buffer.getvalue()


def _detect_max_volume(path: Path) -> float:
    """用 ffmpeg volumedetect 检测音频最大音量（dB）。"""
    result = subprocess.run(
        ["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    match = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", result.stderr)
    if not match:
        return -999.0
    return float(match.group(1))


async def _create_project(async_client, name: str) -> str:
    """创建项目并返回 ID。"""
    response = await async_client.post("/api/projects", json={"name": name})
    assert response.status_code == 200
    return response.json()["id"]


async def _upload_voice(async_client, project_id: str, speaker_name: str) -> dict:
    """上传录音并等待克隆完成（ASGI 下后台任务在响应前执行完毕）。"""
    wav_bytes = _make_tone_wav()
    response = await async_client.post(
        "/api/voice/upload",
        data={"project_id": project_id, "speaker_name": speaker_name, "language": "0"},
        files={"audio": ("recording.wav", wav_bytes, "audio/wav")},
    )
    assert response.status_code == 200, response.text
    project = (await async_client.get(f"/api/projects/{project_id}")).json()
    assert project["voice_status"] == "ready", project.get("voice_error")
    return project


@pytest.mark.asyncio
async def test_raw_recording_saved_as_real_wav(async_client):
    """上传后 raw_recording.wav 应为真实 WAV（RIFF 头），可直接用于试听。"""
    project_id = await _create_project(async_client, "录音转WAV测试")
    await _upload_voice(async_client, project_id, "录音测试音色")

    wav_path = UPLOAD_ROOT / project_id / "raw_recording.wav"
    assert wav_path.exists()
    header = wav_path.read_bytes()[:4]
    assert header == b"RIFF", f"非 WAV 文件: {header}"


@pytest.mark.asyncio
async def test_voices_list_contains_builtin_and_clone(async_client):
    """音色列表应同时包含内置音色与克隆音色，并附原音试听地址。"""
    project_id = await _create_project(async_client, "音色列表测试")
    await _upload_voice(async_client, project_id, "列表测试音色")

    response = await async_client.get("/api/voice/voices")
    assert response.status_code == 200
    voices = response.json()

    # 内置音色：builtin: 前缀、试听地址指向预生成示例（存在时）、无项目归属
    builtin_voices = [v for v in voices if v["is_builtin"]]
    assert len(builtin_voices) == 2
    assert all(v["speaker_id"].startswith("builtin:") for v in builtin_voices)
    for v in builtin_voices:
        spk_id = v["speaker_id"][len("builtin:"):]
        expected_url = (
            f"/api/voice/builtin-preview/{spk_id}"
            if get_builtin_preview_path(spk_id).exists()
            else None
        )
        assert v["raw_recording_url"] == expected_url
    assert {v["speaker_name"] for v in builtin_voices} == {
        "晓晓（女声·温柔）", "云希（男声·阳光）"
    }

    # 克隆音色：clone_ 前缀、附试听地址
    matched = [v for v in voices if v["project_id"] == project_id]
    assert len(matched) == 1
    voice = matched[0]
    assert voice["speaker_id"].startswith("clone_")
    assert voice["is_builtin"] is False
    assert voice["speaker_name"] == "列表测试音色"
    assert voice["raw_recording_url"] == f"/uploads/{project_id}/raw_recording.wav"
    # 克隆时间应带 UTC 时区标记，供前端换算为北京时间（东八区）
    assert datetime.fromisoformat(voice["created_at"]).utcoffset() == timedelta(0)

    # 试听地址应可访问且为 WAV
    audio_response = await async_client.get(voice["raw_recording_url"])
    assert audio_response.status_code == 200
    assert audio_response.headers["content-type"].startswith("audio/wav")


@pytest.mark.asyncio
async def test_voices_list_no_duplicate_speaker(async_client):
    """同一 speaker_id 在列表中只出现一次（项目选择历史音色后会同步 ready）。"""
    source_project = await _create_project(async_client, "去重来源项目")
    source = await _upload_voice(async_client, source_project, "去重测试音色")

    # 另一个项目通过 TTS 选择该音色，voice_status 同步为 ready
    other_project = await _create_project(async_client, "去重使用项目")
    response = await async_client.post(
        "/api/tts/generate",
        json={
            "project_id": other_project,
            "text": "去重测试",
            "speaker_id": source["speaker_id"],
        },
    )
    assert response.status_code == 200

    voices = (await async_client.get("/api/voice/voices")).json()
    clone_ids = [v["speaker_id"] for v in voices if not v["is_builtin"]]
    assert clone_ids.count(source["speaker_id"]) == 1


@pytest.mark.asyncio
async def test_tts_with_selected_speaker(async_client):
    """未克隆音色的新项目可直接指定历史克隆音色进行 TTS。"""
    source_project = await _create_project(async_client, "音色来源项目")
    source = await _upload_voice(async_client, source_project, "被选音色")

    new_project = await _create_project(async_client, "TTS选择音色测试")
    response = await async_client.post(
        "/api/tts/generate",
        json={
            "project_id": new_project,
            "text": "使用历史音色合成语音",
            "speaker_id": source["speaker_id"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["tts_url"].startswith("/uploads/")

    # 新项目应同步所选音色并视为就绪
    project = (await async_client.get(f"/api/projects/{new_project}")).json()
    assert project["voice_status"] == "ready"
    assert project["speaker_id"] == source["speaker_id"]
    assert project["speaker_name"] == "被选音色"


@pytest.mark.asyncio
async def test_generate_without_speaker_id_falls_back_to_source(async_client):
    """
    项目绑定历史克隆音色后，未显式传 speaker_id 的合成应回溯到音色来源项目。

    选择历史音色的新项目本身没有录音，若只查本项目录音会误报"参考录音缺失"。
    """
    source_project = await _create_project(async_client, "回溯来源项目")
    source = await _upload_voice(async_client, source_project, "回溯测试音色")

    target_project = await _create_project(async_client, "回溯目标项目")
    first = await async_client.post(
        "/api/tts/generate",
        json={
            "project_id": target_project,
            "text": "首次绑定历史音色",
            "speaker_id": source["speaker_id"],
        },
    )
    assert first.status_code == 200, first.text
    # 目标项目自身并不产生录音
    assert not (UPLOAD_ROOT / target_project / "raw_recording.wav").exists()

    # 再次合成且不传 speaker_id：应回溯到来源项目录音而非报缺失
    second = await async_client.post(
        "/api/tts/generate",
        json={"project_id": target_project, "text": "复用绑定音色"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_tts_with_builtin_speaker(async_client):
    """未克隆的项目可直接选择内置音色进行 TTS。"""
    project_id = await _create_project(async_client, "内置音色TTS测试")
    response = await async_client.post(
        "/api/tts/generate",
        json={
            "project_id": project_id,
            "text": "使用内置音色合成语音",
            "speaker_id": "builtin:xiaoxiao",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["tts_url"].startswith("/uploads/")

    # 项目应记录内置音色并视为就绪
    project = (await async_client.get(f"/api/projects/{project_id}")).json()
    assert project["voice_status"] == "ready"
    assert project["speaker_id"] == "builtin:xiaoxiao"
    assert project["speaker_name"] == "xiaoxiao"


@pytest.mark.asyncio
async def test_tts_with_invalid_speaker(async_client):
    """不存在的克隆音色 speaker_id 应返回 400。"""
    project_id = await _create_project(async_client, "非法音色测试")
    response = await async_client.post(
        "/api/tts/generate",
        json={
            "project_id": project_id,
            "text": "测试非法音色",
            "speaker_id": "clone_not_exist",
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_tts_is_audible(async_client):
    """TTS 产物应为可听音频（最大音量高于 -50dB，非静音）。"""
    project_id = await _create_project(async_client, "TTS有声测试")
    await _upload_voice(async_client, project_id, "有声测试音色")

    response = await async_client.post(
        "/api/tts/generate",
        json={"project_id": project_id, "text": "你好，这是用于验证语音可听性的测试文本。"},
    )
    assert response.status_code == 200, response.text
    tts_path = UPLOAD_ROOT / project_id / "tts.mp3"
    assert tts_path.exists()

    max_volume = _detect_max_volume(tts_path)
    assert max_volume > -50.0, f"TTS 音频疑似静音: max_volume={max_volume}dB"


@pytest.mark.asyncio
async def test_legacy_invalid_recording_auto_repaired(async_client):
    """
    遗留的非法录音（.wav 扩展名但内容实为 WebM）应在合成前自动转码修复。

    早期版本直接保存浏览器录制的 WebM 内容，推理服务的 soundfile
    解码会报 "Format not recognised" 导致合成失败；此处验证自愈逻辑。
    """
    project_id = await _create_project(async_client, "遗留录音修复测试")
    await _upload_voice(async_client, project_id, "遗留音色")

    wav_path = UPLOAD_ROOT / project_id / "raw_recording.wav"
    assert wav_path.read_bytes()[:4] == b"RIFF"

    # 构造遗留数据：用 WebM(opus) 内容覆盖 WAV，扩展名保持不变
    webm_tmp = wav_path.with_name("legacy.webm")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-c:a", "libopus", "-f", "webm", str(webm_tmp),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    )
    webm_tmp.replace(wav_path)
    assert wav_path.read_bytes()[:4] == b"\x1a\x45\xdf\xa3"

    # 合成时应自动转码修复并成功
    response = await async_client.post(
        "/api/tts/generate", json={"project_id": project_id, "text": "遗留录音修复测试"}
    )
    assert response.status_code == 200, response.text
    assert wav_path.read_bytes()[:4] == b"RIFF"
    # 转码中间文件应被清理
    assert list(wav_path.parent.glob("*.repair_*")) == []


class _FakeErrorResponse:
    """模拟推理服务返回的非 JSON 错误响应（正文为空）。"""

    status_code = 500
    text = ""

    def json(self):
        """模拟对空正文调用 json() 触发解码异常。"""
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


class _FakeErrorClient:
    """模拟 httpx.AsyncClient：异步上下文 + post 返回错误响应。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        """返回模拟的错误响应。"""
        return _FakeErrorResponse()


@pytest.mark.asyncio
async def test_cosyvoice_non_json_error_surfaces_status(monkeypatch, tmp_path):
    """
    推理服务返回非 JSON 的 500 时应抛出可读错误，而非 JSONDecodeError。

    否则真实失败原因（如参考音频格式非法）会被解析异常掩盖。
    """
    from app.services import tts as tts_service

    monkeypatch.setattr(tts_service, "_get_cosyvoice_client", lambda: _FakeErrorClient())

    with pytest.raises(RuntimeError) as excinfo:
        await real_call_cosyvoice_tts("文本", None, "ref.wav", tmp_path / "out.wav")

    message = str(excinfo.value)
    assert "HTTP 500" in message
    assert "JSONDecodeError" not in message
