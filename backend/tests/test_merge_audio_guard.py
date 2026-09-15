"""
合并路由的音频超长检测测试

验证 TTS 音频时长超过视频时长时：
1. 默认请求被 400 拦截并给出可操作提示（避免语音被静默截断）；
2. force_merge=true 时放行合并，产物时长以视频为准；
3. 音频不超过视频时长时无需 force_merge 直接合并。
"""

import subprocess

from httpx import AsyncClient
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models import Project
from app.services.file_store import get_project_file_path
from app.services.media_merge import get_media_duration


def _generate_video(project_id: str, duration: float) -> None:
    """生成指定时长的无声测试视频。"""
    video_path = get_project_file_path(project_id, "video.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"testsrc=duration={duration}:size=640x360:rate=30",
        "-pix_fmt", "yuv420p",
        str(video_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def _generate_audio(project_id: str, duration: float) -> None:
    """生成指定时长的正弦波测试音频。"""
    audio_path = get_project_file_path(project_id, "tts.mp3")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"sine=frequency=1000:duration={duration}",
        "-q:a", "4",
        str(audio_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


async def _mark_project_ready(project_id: str) -> None:
    """将项目视频与 TTS 阶段直接置为就绪（绕过真实生成流程）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one()
        project.video_status = "ready"
        project.video_path = f"/uploads/{project_id}/video.mp4"
        project.tts_status = "ready"
        project.tts_path = f"/uploads/{project_id}/tts.mp3"
        await session.commit()


async def _create_project(client: AsyncClient) -> str:
    """通过 API 创建测试项目并返回 ID。"""
    response = await client.post("/api/projects", json={"name": "音频超长检测测试"})
    response.raise_for_status()
    return response.json()["id"]


async def test_merge_audio_longer_than_video_blocked(async_client: AsyncClient):
    """音频长于视频且未确认时，合并应被 400 拦截。"""
    project_id = await _create_project(async_client)
    _generate_video(project_id, 3)
    _generate_audio(project_id, 8)
    await _mark_project_ready(project_id)

    response = await async_client.post(
        "/api/merge",
        data={"project_id": project_id, "bgm_volume": "0.2", "fade_in": "1", "fade_out": "2"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "超过视频时长" in detail
    assert "force_merge" in detail


async def test_merge_audio_longer_force_merge_succeeds(async_client: AsyncClient):
    """force_merge=true 时放行合并，产物时长以视频为准。"""
    project_id = await _create_project(async_client)
    _generate_video(project_id, 3)
    _generate_audio(project_id, 8)
    await _mark_project_ready(project_id)

    response = await async_client.post(
        "/api/merge",
        data={
            "project_id": project_id,
            "bgm_volume": "0.2",
            "fade_in": "1",
            "fade_out": "2",
            "force_merge": "true",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"

    final_path = get_project_file_path(project_id, "final.mp4")
    assert final_path.exists()
    # 合并以视频时长为准：8 秒音频截断到约 3 秒
    assert 2.5 <= get_media_duration(final_path) <= 3.5


async def test_merge_audio_not_longer_passes_without_force(async_client: AsyncClient):
    """音频不超过视频时长时，无需 force_merge 直接合并成功。"""
    project_id = await _create_project(async_client)
    _generate_video(project_id, 5)
    _generate_audio(project_id, 4)
    await _mark_project_ready(project_id)

    response = await async_client.post(
        "/api/merge",
        data={"project_id": project_id, "bgm_volume": "0.2", "fade_in": "1", "fade_out": "2"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
