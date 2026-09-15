"""
集成测试脚本：实际调用火山 API 验证声音克隆与文生图

用法：
    conda activate av-clone
    python tests/integration_test.py
"""

import time
from pathlib import Path

import httpx

BASE_URL = "http://127.0.0.1:8000"
TEST_VOICE = Path(__file__).resolve().parent.parent.parent / "uploads" / "test_voice.wav"


def create_project():
    """创建测试项目。"""
    response = httpx.post(f"{BASE_URL}/api/projects", json={"name": "声音克隆测试"})
    response.raise_for_status()
    data = response.json()
    print(f"创建项目: {data['id']}")
    return data["id"]


def upload_voice(project_id: str):
    """上传音频并克隆音色。"""
    with open(TEST_VOICE, "rb") as f:
        files = {"audio": ("test_voice.wav", f, "audio/wav")}
        data = {
            "project_id": project_id,
            "speaker_name": "测试音色",
            "language": "0",
        }
        response = httpx.post(f"{BASE_URL}/api/voice/upload", data=data, files=files)
    print(f"上传音频响应: {response.status_code}")
    print(response.text)
    response.raise_for_status()
    return response.json()


def generate_image(project_id: str):
    """调用文生图接口。"""
    response = httpx.post(
        f"{BASE_URL}/api/image/generate",
        json={
            "project_id": project_id,
            "prompt": "一只可爱的橘猫坐在窗台上，阳光照射，温馨治愈，写实风格",
            "size": "2K",
        },
    )
    print(f"文生图响应: {response.status_code}")
    print(response.text)
    response.raise_for_status()
    return response.json()


def get_project(project_id: str):
    """查询项目状态。"""
    response = httpx.get(f"{BASE_URL}/api/projects/{project_id}")
    response.raise_for_status()
    return response.json()


def main():
    """主流程。"""
    if not TEST_VOICE.exists():
        print(f"测试音频不存在: {TEST_VOICE}")
        print("请先运行 ffmpeg 生成 test_voice.wav")
        return

    project_id = create_project()

    print("\n=== 开始声音克隆 ===")
    upload_voice(project_id)

    print("\n=== 开始文生图 ===")
    generate_image(project_id)

    print("\n=== 等待后台任务 ===")
    for i in range(20):
        time.sleep(3)
        project = get_project(project_id)
        print(
            f"第 {i+1} 次轮询 - 音色: {project['voice_status']}, "
            f"图片: {project['image_status']}, "
            f"speaker_id: {project.get('speaker_id')}"
        )
        if project["voice_status"] in ("ready", "failed") and project["image_status"] in (
            "ready",
            "failed",
        ):
            break

    print("\n=== 最终状态 ===")
    project = get_project(project_id)
    print(f"voice_status: {project['voice_status']}")
    print(f"voice_error: {project.get('voice_error')}")
    print(f"speaker_id: {project.get('speaker_id')}")
    print(f"image_status: {project['image_status']}")
    print(f"image_error: {project.get('image_error')}")
    print(f"image_path: {project.get('image_path')}")


if __name__ == "__main__":
    main()
