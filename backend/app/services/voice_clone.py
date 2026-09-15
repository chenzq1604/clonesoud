"""
本地声音克隆服务（CosyVoice3 零样本）

CosyVoice3 为零样本克隆架构：无需提交训练任务，录音本身即是音色。
本模块负责将上传录音统一转换为标准 WAV 保存，并生成本地音色标识。
"""

import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from app.services.file_store import get_project_dir


def _convert_audio_to_wav(input_path: Path, output_path: Path) -> Path:
    """
    使用 FFmpeg 将任意格式录音统一转换为 16kHz 16bit 单声道 WAV。

    浏览器 MediaRecorder 录制的是 webm 容器，直接以 .wav 扩展名保存
    会导致前端 <audio> 按 audio/wav 解析失败无法试听；
    CosyVoice 推理服务亦要求 16kHz WAV 作为参考音频。

    Args:
        input_path: 原始音频文件路径。
        output_path: 输出 WAV 路径。

    Returns:
        转换后的 WAV 路径。

    Raises:
        RuntimeError: FFmpeg 转换失败。
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-ar", "16000",
        "-ac", "1",
        "-acodec", "pcm_s16le",
        # 显式指定容器格式：临时文件扩展名可能无法被 FFmpeg 推断
        "-f", "wav",
        str(output_path),
    ]
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False,
    )
    if result.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"录音转 WAV 失败: {result.stderr}")
    return output_path


async def save_raw_recording_as_wav(project_id: str, audio_bytes: bytes) -> Path:
    """
    将上传录音统一转换为标准 WAV 并保存到项目目录。

    Args:
        project_id: 项目 ID。
        audio_bytes: 上传的原始音频二进制（webm/mp3/wav 均可）。

    Returns:
        保存后的 raw_recording.wav 路径。
    """

    def _convert() -> Path:
        project_dir = get_project_dir(project_id)
        # 先落盘原始字节（扩展名按需选择，ffmpeg 依内容探测格式）
        source_ext = ".bin"
        source_path = project_dir / f"_raw_upload{source_ext}"
        source_path.write_bytes(audio_bytes)
        target_path = project_dir / "raw_recording.wav"
        try:
            return _convert_audio_to_wav(source_path, target_path)
        finally:
            source_path.unlink(missing_ok=True)

    return await asyncio.to_thread(_convert)


def is_valid_wav(path: Path) -> bool:
    """
    判断文件是否为标准 WAV（RIFF 头）。

    Args:
        path: 待检测文件路径。

    Returns:
        文件存在且以 RIFF 头起始返回 True，否则 False。
    """
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"RIFF"
    except OSError:
        return False


def _repair_recording_to_wav(path: Path) -> None:
    """
    将非法格式的录音文件就地转码为标准 WAV。

    先复制到临时文件再转码，避免 FFmpeg 同时读写同一路径；
    转码成功后用产物原子替换原文件。

    Args:
        path: 待修复的录音路径。

    Raises:
        RuntimeError: 转码失败（如文件损坏、无音频轨）。
    """
    tmp_src = path.with_name(path.name + ".repair_src")
    tmp_dst = path.with_name(path.name + ".repair_dst")
    shutil.copyfile(path, tmp_src)
    try:
        _convert_audio_to_wav(tmp_src, tmp_dst)
        tmp_dst.replace(path)
    finally:
        tmp_src.unlink(missing_ok=True)
        tmp_dst.unlink(missing_ok=True)


async def ensure_reference_wav(path: Path) -> Path:
    """
    确保参考录音为可解码的标准 WAV，非法格式就地转码自愈。

    早期版本可能把浏览器录制的 WebM 内容直接以 .wav 扩展名保存，
    推理服务的 soundfile 解码会报 "Format not recognised"；
    此处在使用前做一次性修复，避免每次合成都失败。

    Args:
        path: 参考录音路径。

    Returns:
        修复后的同一路径。

    Raises:
        RuntimeError: 文件不存在或转码失败。
    """
    if not path.exists():
        raise RuntimeError(f"参考录音不存在: {path}")
    if is_valid_wav(path):
        return path
    await asyncio.to_thread(_repair_recording_to_wav, path)
    return path


async def clone_voice(
    project_id: str,
    audio_bytes: bytes,
    speaker_name: Optional[str],
    language: int = 0,
) -> str:
    """
    保存录音并创建本地克隆音色标识。

    CosyVoice3 为零样本架构，录音即为音色：保存参考音频后
    立即可用于合成，无需任何训练等待。speaker_id 仅为本地记录用。

    Args:
        project_id: 项目 ID，用于本地文件归类。
        audio_bytes: 原始音频二进制数据（webm/mp3/wav 均可，内部统一转 WAV）。
        speaker_name: 用户自定义音色名称（本地记录用）。
        language: 语言代码（保留参数以兼容既有接口，零样本克隆不依赖）。

    Returns:
        本地音色标识（clone_ 前缀 + 随机串）。

    Raises:
        RuntimeError: 录音转换失败时抛出。
    """
    await save_raw_recording_as_wav(project_id, audio_bytes)
    return f"clone_{uuid.uuid4().hex[:16]}"


async def wait_for_clone_ready(
    speaker_id: str,
    max_attempts: int = 60,
    interval: float = 2.0,
) -> bool:
    """
    等待音色就绪（零样本克隆即时完成，直接返回成功）。

    Args:
        speaker_id: 音色标识。
        max_attempts: 保留参数以兼容既有接口。
        interval: 保留参数以兼容既有接口。

    Returns:
        恒为 True。
    """
    return True
