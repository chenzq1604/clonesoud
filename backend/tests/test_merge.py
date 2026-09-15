"""
FFmpeg 合并功能测试（含音频模式：替换 / 叠加）
"""

import re
import subprocess

import pytest

from app.services.file_store import get_project_file_path, save_upload_file
from app.services.media_merge import get_media_duration, merge_final_video


def _generate_test_video(project_id: str) -> None:
    """生成 3 秒 1280x720 无声测试视频。"""
    video_path = get_project_file_path(project_id, "video.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "testsrc=duration=3:size=1280x720:rate=30",
        "-pix_fmt", "yuv420p",
        str(video_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def _generate_test_video_with_audio(project_id: str) -> None:
    """生成 3 秒带 3000Hz 原声的测试视频（用于区分原声与 TTS）。"""
    video_path = get_project_file_path(project_id, "video.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=3000:duration=3",
        "-map", "0:v", "-map", "1:a",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264", "-c:a", "aac",
        str(video_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def _generate_test_audio(project_id: str) -> None:
    """生成 3 秒 1000Hz 正弦波音频。"""
    audio_path = get_project_file_path(project_id, "tts.mp3")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "sine=frequency=1000:duration=3",
        "-q:a", "4",
        str(audio_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def _bandpass_max_volume(path, freq: float) -> float:
    """检测音频中指定频率成分的最大音量（dB），用于验证原声是否保留。"""
    result = subprocess.run(
        [
            "ffmpeg", "-i", str(path),
            "-af", f"bandpass=f={freq}:w=400,volumedetect",
            "-f", "null", "-",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    match = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", result.stderr)
    return float(match.group(1)) if match else -999.0


@pytest.mark.asyncio
async def test_get_media_duration():
    """测试 ffprobe 获取媒体时长。"""
    project_id = "test_duration"
    _generate_test_video(project_id)
    video_path = get_project_file_path(project_id, "video.mp4")
    duration = get_media_duration(video_path)
    assert 2.5 <= duration <= 3.5


@pytest.mark.asyncio
async def test_merge_without_bgm():
    """测试无 BGM 的合并。"""
    project_id = "test_merge_no_bgm"
    _generate_test_video(project_id)
    _generate_test_audio(project_id)

    final_url = merge_final_video(project_id)
    final_path = get_project_file_path(project_id, "final.mp4")

    assert final_path.exists()
    assert final_url == f"/uploads/{project_id}/final.mp4"

    duration = get_media_duration(final_path)
    assert 2.5 <= duration <= 3.5


@pytest.mark.asyncio
async def test_merge_with_bgm():
    """测试带 BGM 的合并。"""
    project_id = "test_merge_with_bgm"
    _generate_test_video(project_id)
    _generate_test_audio(project_id)

    # 生成 BGM
    bgm_path = get_project_file_path(project_id, "bgm.mp3")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "sine=frequency=500:duration=1",
        "-q:a", "4",
        str(bgm_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    final_url = merge_final_video(project_id, bgm_path=bgm_path, bgm_volume=0.3)
    final_path = get_project_file_path(project_id, "final.mp4")

    assert final_path.exists()
    assert final_url == f"/uploads/{project_id}/final.mp4"


# ========== 音频模式：替换 / 叠加 ==========


@pytest.mark.asyncio
async def test_merge_overlay_keeps_original_audio():
    """叠加模式应保留视频原声并与语音混合。"""
    project_id = "test_overlay_with_audio"
    _generate_test_video_with_audio(project_id)  # 原声 3000Hz
    _generate_test_audio(project_id)  # 语音 1000Hz

    merge_final_video(project_id, fade_in=0.1, fade_out=0.1, audio_mode="overlay")

    final_path = get_project_file_path(project_id, "final.mp4")
    assert final_path.exists()
    # 原声（3000Hz）与语音（1000Hz）应同时保留
    assert _bandpass_max_volume(final_path, 3000) > -40, "叠加模式丢失了视频原声"
    assert _bandpass_max_volume(final_path, 1000) > -40, "叠加模式丢失了合成语音"


@pytest.mark.asyncio
async def test_merge_replace_discards_original_audio():
    """替换模式应丢弃视频原声，仅保留合成语音。"""
    project_id = "test_replace_with_audio"
    _generate_test_video_with_audio(project_id)  # 原声 3000Hz
    _generate_test_audio(project_id)  # 语音 1000Hz

    merge_final_video(project_id, fade_in=0.1, fade_out=0.1, audio_mode="replace")

    final_path = get_project_file_path(project_id, "final.mp4")
    assert final_path.exists()
    # 原声（3000Hz）应接近静音，语音（1000Hz）应保留
    assert _bandpass_max_volume(final_path, 3000) < -50, "替换模式未丢弃视频原声"
    assert _bandpass_max_volume(final_path, 1000) > -40, "替换模式丢失了合成语音"


@pytest.mark.asyncio
async def test_merge_overlay_without_audio_falls_back():
    """视频无原声时叠加模式应回退为替换并正常完成。"""
    project_id = "test_overlay_no_audio"
    _generate_test_video(project_id)  # 无声视频
    _generate_test_audio(project_id)

    final_url = merge_final_video(project_id, fade_in=0.1, fade_out=0.1, audio_mode="overlay")

    final_path = get_project_file_path(project_id, "final.mp4")
    assert final_path.exists()
    assert final_url == f"/uploads/{project_id}/final.mp4"
    assert _bandpass_max_volume(final_path, 1000) > -40


@pytest.mark.asyncio
async def test_merge_overlay_with_bgm():
    """叠加模式 + BGM：三路音频（原声/语音/BGM）混合应正常完成。"""
    project_id = "test_overlay_with_bgm"
    _generate_test_video_with_audio(project_id)
    _generate_test_audio(project_id)

    bgm_path = get_project_file_path(project_id, "bgm.mp3")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "sine=frequency=500:duration=1",
        "-q:a", "4",
        str(bgm_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

    final_url = merge_final_video(
        project_id, bgm_path=bgm_path, bgm_volume=0.3,
        fade_in=0.1, fade_out=0.1, audio_mode="overlay",
    )

    final_path = get_project_file_path(project_id, "final.mp4")
    assert final_path.exists()
    assert final_url == f"/uploads/{project_id}/final.mp4"
    # 三路音频都应存在
    assert _bandpass_max_volume(final_path, 3000) > -40
    assert _bandpass_max_volume(final_path, 1000) > -40
    assert _bandpass_max_volume(final_path, 500) > -40
