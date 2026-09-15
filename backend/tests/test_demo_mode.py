"""
演示模式功能测试

验证 demo_mode=true 时，全流程不依赖外部 API 即可生成最终视频。
"""

import time
from pathlib import Path

import httpx
import pytest

from app.config import settings

BASE_URL = "http://127.0.0.1:8000"
TEST_VOICE = Path(__file__).resolve().parent.parent.parent / "uploads" / "test_voice.wav"

# 本文件为端到端集成测试，要求运行中的服务处于 DEMO_MODE=true；
# 真实模式下会调用火山外部 API（依赖有效凭据与计费），不适合自动执行
pytestmark = pytest.mark.skipif(
    not settings.demo_mode,
    reason="需要服务以 DEMO_MODE=true 运行（真实模式会调用外部计费 API）",
)


def create_project():
    """创建测试项目并返回 ID。"""
    response = httpx.post(f"{BASE_URL}/api/projects", json={"name": "演示模式测试"})
    response.raise_for_status()
    return response.json()["id"]


def get_project(project_id: str):
    """查询项目状态。"""
    response = httpx.get(f"{BASE_URL}/api/projects/{project_id}")
    response.raise_for_status()
    return response.json()


@pytest.mark.skipif(not TEST_VOICE.exists(), reason="测试音频不存在")
def test_voice_clone_demo():
    """上传音频后应在 demo 模式下完成音色克隆。"""
    project_id = create_project()
    with open(TEST_VOICE, "rb") as f:
        files = {"audio": ("test_voice.wav", f, "audio/wav")}
        data = {
            "project_id": project_id,
            "speaker_name": "演示音色",
            "language": "0",
        }
        response = httpx.post(f"{BASE_URL}/api/voice/upload", data=data, files=files)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cloning"

    # 等待后台任务完成
    for _ in range(20):
        project = get_project(project_id)
        if project["voice_status"] in ("ready", "failed"):
            break
        time.sleep(0.5)
    assert project["voice_status"] == "ready"
    assert project["speaker_id"]


def test_image_generate_and_select_demo():
    """文生图应在 demo 模式下返回本地占位图并支持选择。"""
    project_id = create_project()
    response = httpx.post(
        f"{BASE_URL}/api/image/generate",
        json={
            "project_id": project_id,
            "prompt": "演示模式测试",
            "size": "1024x1024",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert len(body["image_urls"]) >= 1
    assert all(url.startswith("/uploads/") for url in body["image_urls"])

    select_response = httpx.post(
        f"{BASE_URL}/api/image/select",
        json={"project_id": project_id, "selected_index": 0},
    )
    assert select_response.status_code == 200
    assert select_response.json()["selected_url"].startswith("/uploads/")


def _cosyvoice_available() -> bool:
    """检查本地 CosyVoice 推理服务是否可用且模型已加载。"""
    try:
        response = httpx.get("http://127.0.0.1:9880/health", timeout=5.0)
        return response.status_code == 200 and response.json().get("status") == "ok"
    except Exception:
        return False


def test_tts_demo():
    """TTS 应通过本地 CosyVoice 服务生成音频（服务未启动时跳过）。"""
    if not _cosyvoice_available():
        pytest.skip("本地 CosyVoice 推理服务未运行")
    project_id = create_project()
    # 先完成声音克隆（本地零样本，即时就绪）
    assert TEST_VOICE.exists()
    with open(TEST_VOICE, "rb") as f:
        httpx.post(
            f"{BASE_URL}/api/voice/upload",
            data={"project_id": project_id, "speaker_name": "演示音色", "language": "0"},
            files={"audio": ("test_voice.wav", f, "audio/wav")},
        )
    for _ in range(20):
        project = get_project(project_id)
        if project["voice_status"] in ("ready", "failed"):
            break
        time.sleep(0.5)
    assert project["voice_status"] == "ready"

    response = httpx.post(
        f"{BASE_URL}/api/tts/generate",
        json={"project_id": project_id, "text": "这是本地语音合成的测试文本。"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["tts_url"].startswith("/uploads/")


def test_video_generate_demo():
    """图生视频在 demo 模式下应基于首帧图生成占位视频。"""
    project_id = create_project()
    # 先完成文生图并选择
    httpx.post(
        f"{BASE_URL}/api/image/generate",
        json={"project_id": project_id, "prompt": "演示视频首帧", "size": "1024x1024"},
    )
    httpx.post(
        f"{BASE_URL}/api/image/select",
        json={"project_id": project_id, "selected_index": 0},
    )

    response = httpx.post(
        f"{BASE_URL}/api/video/generate",
        json={"project_id": project_id, "prompt": "镜头缓缓推进", "mode": "image_to_video"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "generating"

    for _ in range(20):
        project = get_project(project_id)
        if project["video_status"] in ("ready", "failed"):
            break
        time.sleep(0.5)
    assert project["video_status"] == "ready"
    assert project["video_path"].startswith("/uploads/")


def test_full_demo_pipeline():
    """完整 demo 流程：声音克隆 -> TTS -> 文生图 -> 图生视频 -> 合并。"""
    project_id = create_project()

    # 1. 声音克隆
    assert TEST_VOICE.exists()
    with open(TEST_VOICE, "rb") as f:
        httpx.post(
            f"{BASE_URL}/api/voice/upload",
            data={"project_id": project_id, "speaker_name": "演示音色", "language": "0"},
            files={"audio": ("test_voice.wav", f, "audio/wav")},
        )

    # 等待音色克隆完成后再继续
    for _ in range(20):
        project = get_project(project_id)
        if project["voice_status"] in ("ready", "failed"):
            break
        time.sleep(0.5)
    assert project["voice_status"] == "ready"

    # 2. 文生图并选择
    httpx.post(
        f"{BASE_URL}/api/image/generate",
        json={"project_id": project_id, "prompt": "演示视频首帧", "size": "1024x1024"},
    )
    httpx.post(
        f"{BASE_URL}/api/image/select",
        json={"project_id": project_id, "selected_index": 0},
    )

    # 3. TTS
    httpx.post(
        f"{BASE_URL}/api/tts/generate",
        json={"project_id": project_id, "text": "这是演示模式完整流程测试。"},
    )

    # 4. 图生视频
    httpx.post(
        f"{BASE_URL}/api/video/generate",
        json={"project_id": project_id, "prompt": "镜头缓缓推进", "mode": "image_to_video"},
    )

    # 等待所有后台任务完成
    for _ in range(40):
        project = get_project(project_id)
        if (
            project["voice_status"] in ("ready", "failed")
            and project["tts_status"] in ("ready", "failed")
            and project["video_status"] in ("ready", "failed")
        ):
            break
        time.sleep(0.5)

    assert project["voice_status"] == "ready"
    assert project["tts_status"] == "ready"
    assert project["video_status"] == "ready"
    assert project["image_path"]

    # 5. 合并最终视频（merge 路由使用 multipart/form-data）
    merge_response = httpx.post(
        f"{BASE_URL}/api/merge",
        data={
            "project_id": project_id,
            "bgm_volume": "0.2",
            "fade_in": "1",
            "fade_out": "1",
        },
    )
    print(f"merge status: {merge_response.status_code}")
    print(f"merge response: {merge_response.text}")
    assert merge_response.status_code == 200
    body = merge_response.json()
    assert body["status"] == "ready"
    assert body["final_url"].startswith("/uploads/")

    # 确认最终文件存在且可访问
    preview_response = httpx.get(f"{BASE_URL}{body['final_url']}")
    assert preview_response.status_code == 200
    assert preview_response.headers["content-type"] == "video/mp4"
