"""
边界条件与错误处理测试

验证各接口在异常输入或前置条件不满足时返回正确的错误码与提示。
"""

import time
from pathlib import Path

import httpx
import pytest

# 直连 conftest 启动的独立测试后端（使用测试库与测试上传目录）
BASE_URL = "http://127.0.0.1:8377"
TEST_VOICE = Path(__file__).resolve().parent / "_test_voice.wav"

# 每个测试依赖独立后端进程（session 级 fixture）
pytestmark = pytest.mark.usefixtures("edge_case_backend")


def create_project(name: str = "边界测试") -> str:
    """创建测试项目并返回 ID。"""
    response = httpx.post(f"{BASE_URL}/api/projects", json={"name": name})
    response.raise_for_status()
    return response.json()["id"]


def get_project(project_id: str) -> dict:
    """查询项目状态。"""
    response = httpx.get(f"{BASE_URL}/api/projects/{project_id}")
    response.raise_for_status()
    return response.json()


def wait_for_status(project_id: str, field: str, timeout: int = 20) -> str:
    """轮询等待某个阶段完成。"""
    for _ in range(timeout * 2):
        project = get_project(project_id)
        status = project.get(f"{field}_status", "")
        if status in ("ready", "failed"):
            return status
        time.sleep(0.5)
    return "timeout"


# ========== 项目 CRUD 边界 ==========

def test_get_nonexistent_project():
    """查询不存在的项目应返回 404。"""
    response = httpx.get(f"{BASE_URL}/api/projects/nonexistent_id")
    assert response.status_code == 404


def test_delete_nonexistent_project():
    """删除不存在的项目应返回 404。"""
    response = httpx.delete(f"{BASE_URL}/api/projects/nonexistent_id")
    assert response.status_code == 404


def test_create_and_delete_project():
    """创建后删除项目应正常工作。"""
    project_id = create_project("删除测试")
    assert project_id

    # 查询确认存在
    response = httpx.get(f"{BASE_URL}/api/projects/{project_id}")
    assert response.status_code == 200

    # 删除
    delete_response = httpx.delete(f"{BASE_URL}/api/projects/{project_id}")
    assert delete_response.status_code == 200

    # 再查询应 404
    response = httpx.get(f"{BASE_URL}/api/projects/{project_id}")
    assert response.status_code == 404


# ========== 声音克隆边界 ==========

def test_voice_upload_nonexistent_project():
    """向不存在的项目上传音频应返回 404。"""
    if not TEST_VOICE.exists():
        pytest.skip("测试音频不存在")

    with open(TEST_VOICE, "rb") as f:
        response = httpx.post(
            f"{BASE_URL}/api/voice/upload",
            data={"project_id": "nonexistent", "speaker_name": "test", "language": "0"},
            files={"audio": ("test.wav", f, "audio/wav")},
        )
    assert response.status_code == 404


def test_voice_upload_no_audio_file():
    """上传时不带音频文件应返回 422。"""
    project_id = create_project()
    response = httpx.post(
        f"{BASE_URL}/api/voice/upload",
        data={"project_id": project_id, "speaker_name": "test", "language": "0"},
    )
    assert response.status_code == 422


# ========== TTS 边界 ==========

def test_tts_before_voice_ready():
    """音色未就绪时请求 TTS 应返回 400。"""
    project_id = create_project()
    response = httpx.post(
        f"{BASE_URL}/api/tts/generate",
        json={"project_id": project_id, "text": "测试文本"},
    )
    assert response.status_code == 400
    assert "音色尚未准备就绪" in response.json()["detail"]


def test_tts_empty_text():
    """空文本应返回 422（校验失败）。"""
    project_id = create_project()
    response = httpx.post(
        f"{BASE_URL}/api/tts/generate",
        json={"project_id": project_id, "text": ""},
    )
    assert response.status_code == 422


def test_tts_nonexistent_project():
    """向不存在的项目请求 TTS 应返回 404。"""
    response = httpx.post(
        f"{BASE_URL}/api/tts/generate",
        json={"project_id": "nonexistent", "text": "测试"},
    )
    assert response.status_code == 404


# ========== 文生图边界 ==========

def test_image_generate_nonexistent_project():
    """向不存在的项目请求文生图应返回 404。"""
    response = httpx.post(
        f"{BASE_URL}/api/image/generate",
        json={"project_id": "nonexistent", "prompt": "测试"},
    )
    assert response.status_code == 404


def test_image_generate_empty_prompt():
    """空提示词应返回 422。"""
    project_id = create_project()
    response = httpx.post(
        f"{BASE_URL}/api/image/generate",
        json={"project_id": project_id, "prompt": ""},
    )
    assert response.status_code == 422


def test_image_select_invalid_index():
    """选择无效索引应返回 400。"""
    project_id = create_project()
    response = httpx.post(
        f"{BASE_URL}/api/image/select",
        json={"project_id": project_id, "selected_index": 99},
    )
    assert response.status_code == 400


# ========== 视频生成边界 ==========

def test_video_image_mode_before_image_selected():
    """未选择图片时请求图生视频应返回 400。"""
    project_id = create_project()
    response = httpx.post(
        f"{BASE_URL}/api/video/generate",
        json={"project_id": project_id, "prompt": "测试", "mode": "image_to_video"},
    )
    assert response.status_code == 400
    assert "请先生成并选择图片" in response.json()["detail"]


def test_video_invalid_mode():
    """非法生成模式应返回 422。"""
    project_id = create_project()
    response = httpx.post(
        f"{BASE_URL}/api/video/generate",
        json={"project_id": project_id, "prompt": "测试", "mode": "invalid_mode"},
    )
    assert response.status_code == 422


# ========== 合并边界 ==========

def test_merge_before_video_ready():
    """视频未就绪时请求合并应返回 400。"""
    project_id = create_project()
    response = httpx.post(
        f"{BASE_URL}/api/merge",
        data={"project_id": project_id, "bgm_volume": "0.2", "fade_in": "1", "fade_out": "2"},
    )
    assert response.status_code == 400


def test_merge_nonexistent_project():
    """向不存在的项目请求合并应返回 404。"""
    response = httpx.post(
        f"{BASE_URL}/api/merge",
        data={"project_id": "nonexistent", "bgm_volume": "0.2", "fade_in": "1", "fade_out": "2"},
    )
    assert response.status_code == 404


def test_preview_nonexistent_project():
    """预览不存在的项目应返回 404。"""
    response = httpx.get(f"{BASE_URL}/api/merge/preview/nonexistent")
    assert response.status_code == 404


def test_preview_before_merge_ready():
    """合并未完成时预览应返回 400。"""
    project_id = create_project()
    response = httpx.get(f"{BASE_URL}/api/merge/preview/{project_id}")
    assert response.status_code == 400


# ========== 健康检查 ==========

def test_health_check():
    """健康检查应返回 ok。"""
    response = httpx.get(f"{BASE_URL}/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
