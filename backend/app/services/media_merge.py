"""
FFmpeg 媒体合并服务

将生成的视频、克隆 TTS 音频、用户上传的 BGM 合并为最终视频。
支持两种音频模式：替换（丢弃视频原声）与叠加（视频原声与语音混合）。
"""

import json
import subprocess
from pathlib import Path

from app.services.file_store import get_public_url, get_project_file_path

# 支持的音频模式
AUDIO_MODE_REPLACE = "replace"
AUDIO_MODE_OVERLAY = "overlay"


def _run_ffmpeg(cmd: list[str]) -> None:
    """执行 FFmpeg 命令，失败时抛出 RuntimeError。"""
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg 执行失败: {result.stderr}")


def video_has_audio(video_path: Path) -> bool:
    """
    检测视频文件是否包含音频流。

    Args:
        video_path: 视频文件路径。

    Returns:
        存在音频流返回 True；探测失败视为无音频。
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def get_media_duration(media_path: Path) -> float:
    """
    使用 ffprobe 获取媒体文件时长（秒）。

    Args:
        media_path: 媒体文件路径。

    Returns:
        时长秒数，解析失败返回 0.0。
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(media_path),
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0.0
    try:
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0.0))
    except (json.JSONDecodeError, ValueError):
        return 0.0


def merge_final_video(
    project_id: str,
    bgm_path: Path | None = None,
    bgm_volume: float = 0.2,
    fade_in: float = 1.0,
    fade_out: float = 2.0,
    audio_mode: str = AUDIO_MODE_REPLACE,
) -> str:
    """
    合并视频、TTS 音频与可选 BGM。

    Args:
        project_id: 项目 ID。
        bgm_path: BGM 文件路径，可选。
        bgm_volume: BGM 音量比例（相对于 TTS）。
        fade_in: 音频淡入秒数。
        fade_out: 音频淡出秒数。
        audio_mode: 音频模式——replace 替换（丢弃视频原声，
            默认）；overlay 叠加（视频原声与语音混合，
            视频无音频流时自动回退为替换）。

    Returns:
        最终视频本地 URL。
    """
    video_path = get_project_file_path(project_id, "video.mp4")
    tts_path = get_project_file_path(project_id, "tts.mp3")
    final_path = get_project_file_path(project_id, "final.mp4")

    if not video_path.exists():
        raise FileNotFoundError(f"视频文件不存在: {video_path}")
    if not tts_path.exists():
        raise FileNotFoundError(f"TTS 音频不存在: {tts_path}")

    video_duration = get_media_duration(video_path)
    if video_duration <= 0:
        raise ValueError("无法获取视频时长")

    fade_out_start = max(0.0, video_duration - fade_out)

    # 叠加模式仅在视频确有原声时生效，否则回退为替换
    use_overlay = audio_mode == AUDIO_MODE_OVERLAY and video_has_audio(video_path)

    # 基础命令
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(tts_path),
    ]

    if use_overlay:
        # 叠加模式：视频原声与 TTS（及可选 BGM）混合，
        # alimiter 防止多路叠加后超出满幅产生削波
        if bgm_path and bgm_path.exists():
            cmd.extend(["-i", str(bgm_path)])
            filter_complex = (
                f"[0:a]atrim=0:{video_duration},asetpts=PTS-STARTPTS[va];"
                f"[1:a]atrim=0:{video_duration},asetpts=PTS-STARTPTS[tts];"
                f"[2:a]aloop=loop=-1:size=0,atrim=0:{video_duration},asetpts=PTS-STARTPTS,"
                f"volume={bgm_volume}[bgm];"
                f"[va][tts][bgm]amix=inputs=3:duration=first:normalize=0,"
                f"alimiter=limit=0.97,"
                f"afade=t=in:ss=0:d={fade_in},afade=t=out:st={fade_out_start}:d={fade_out}[aout]"
            )
        else:
            filter_complex = (
                f"[0:a]atrim=0:{video_duration},asetpts=PTS-STARTPTS[va];"
                f"[1:a]atrim=0:{video_duration},asetpts=PTS-STARTPTS[tts];"
                f"[va][tts]amix=inputs=2:duration=first:normalize=0,"
                f"alimiter=limit=0.97,"
                f"afade=t=in:ss=0:d={fade_in},afade=t=out:st={fade_out_start}:d={fade_out}[aout]"
            )
        cmd.extend(["-filter_complex", filter_complex, "-map", "[aout]"])
    elif bgm_path and bgm_path.exists():
        # 替换模式 + BGM：三路输入（视频 + TTS + BGM），丢弃视频原声
        cmd.extend(["-i", str(bgm_path)])
        filter_complex = (
            f"[1:a]atrim=0:{video_duration},asetpts=PTS-STARTPTS[tts];"
            f"[2:a]aloop=loop=-1:size=0,atrim=0:{video_duration},asetpts=PTS-STARTPTS,"
            f"volume={bgm_volume},afade=t=in:ss=0:d={fade_in},afade=t=out:st={fade_out_start}:d={fade_out}[bgm];"
            f"[tts][bgm]amix=inputs=2:duration=first:normalize=0[aout]"
        )
        cmd.extend(["-filter_complex", filter_complex, "-map", "[aout]"])
    else:
        # 替换模式：两路输入（视频 + TTS），丢弃视频原声
        filter_complex = (
            f"[1:a]atrim=0:{video_duration},asetpts=PTS-STARTPTS,"
            f"afade=t=in:ss=0:d={fade_in},afade=t=out:st={fade_out_start}:d={fade_out}[aout]"
        )
        cmd.extend(["-filter_complex", filter_complex, "-map", "[aout]"])

    cmd.extend([
        "-map", "0:v:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(final_path),
    ])

    _run_ffmpeg(cmd)
    return get_public_url(project_id, "final.mp4")
