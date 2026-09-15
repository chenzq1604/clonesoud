"""
语音合成（TTS）服务

调用本地 CosyVoice3 推理服务将文本转换为音频。
支持两种音色：内置预置音色（builtin: 前缀）与克隆音色（参考录音零样本合成）。

语速控制策略：CosyVoice 的 speed 参数通过 mel 频谱时间轴线性插值实现，
会破坏谐波结构导致音色漂移（克隆音色在 1.5x/2.0x 下尤其明显）。
因此推理阶段恒定以 1.0x 合成保证音色保真，变速统一由 FFmpeg atempo
滤波器完成（WSOLA 时间伸缩，保持音高与音色）。
"""

import asyncio
import json
import logging
import subprocess
from pathlib import Path

import httpx

from app.config import settings
from app.services.file_store import (
    get_project_dir,
    get_project_file_path,
    get_public_url,
    save_upload_file,
)
from app.services.voice_clone import ensure_reference_wav

logger = logging.getLogger(__name__)

# 内置音色标识前缀，用于与克隆音色区分
BUILTIN_PREFIX = "builtin:"

# 内置音色试听片段路由前缀（供音色列表拼接触听地址）
BUILTIN_PREVIEW_URL_PREFIX = "/api/voice/builtin-preview/"


def get_builtin_preview_path(speaker_id: str) -> Path:
    """
    返回内置音色试听片段文件路径（可能不存在）。

    试听片段由 cosyvoice_service/make_builtin_previews.py 预生成
    （约 2 秒示例语音，与真实合成效果一致），存放于内置音色资源目录
    的 previews/ 子目录下。

    Args:
        speaker_id: 内置音色 ID（不带 builtin: 前缀）。

    Returns:
        试听片段 WAV 文件路径。
    """
    return settings.builtin_voices_root / "previews" / f"{speaker_id}.wav"


def get_builtin_speaker_name(speaker_id: str) -> str | None:
    """
    从预置清单读取内置音色的显示名（如"晓晓（女声·温柔）"）。

    Args:
        speaker_id: 内置音色 ID（不带 builtin: 前缀）。

    Returns:
        显示名；清单缺失或未命中时返回 None。
    """
    try:
        manifest_path = settings.builtin_voices_root / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for v in data.get("voices", []):
            if v.get("id") == speaker_id:
                return str(v.get("name", speaker_id))
    except Exception:
        logger.warning("读取内置音色清单失败，回退使用音色 ID: %s", speaker_id)
    return None


def _get_cosyvoice_client() -> httpx.AsyncClient:
    """创建访问本地 CosyVoice 推理服务的 HTTP 客户端。

    trust_env=False：本地回环地址必须直连，禁止读取环境变量中的
    HTTP_PROXY 等代理配置（进程环境带代理时会导致本机请求被劫持）。
    """
    return httpx.AsyncClient(timeout=settings.cosyvoice_timeout, trust_env=False)


def _run_ffmpeg(cmd: list[str]) -> None:
    """执行 FFmpeg 命令，失败时抛出 RuntimeError。"""
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg 执行失败: {result.stderr}")


def _wav_to_mp3(wav_path: Path, mp3_path: Path, speed: float = 1.0) -> Path:
    """
    将合成 WAV 转换为 24kHz 单声道 MP3（与视频合成管线兼容）。

    speed != 1.0 时通过 atempo 滤波器做保音高变速（WSOLA 时间伸缩），
    替代 CosyVoice 推理侧变速，避免 mel 频谱插值导致的音色漂移。

    Args:
        wav_path: 合成 WAV 路径。
        mp3_path: 输出 MP3 路径。
        speed: 语速系数（0.5 ~ 2.0，1.0 为正常）。

    Returns:
        输出 MP3 路径。
    """
    cmd = ["ffmpeg", "-y", "-i", str(wav_path)]
    if abs(speed - 1.0) > 1e-3:
        cmd += ["-filter:a", f"atempo={speed:.4g}"]
    cmd += [
        "-ar", "24000", "-ac", "1",
        "-acodec", "libmp3lame", "-q:a", "4",
        str(mp3_path),
    ]
    _run_ffmpeg(cmd)
    return mp3_path


async def list_builtin_speakers() -> list[dict]:
    """
    查询内置音色列表（预置参考音频清单）。

    Returns:
        内置音色字典列表（含 id 与 name）；
        推理服务不可用时返回空列表（记录警告日志）。
    """
    try:
        async with _get_cosyvoice_client() as client:
            response = await client.get(f"{settings.cosyvoice_base_url}/api/speakers")
            response.raise_for_status()
            return response.json().get("speakers", [])
    except Exception as exc:
        logger.warning("查询 CosyVoice 内置音色失败: %s", exc)
        return []


async def check_cosyvoice_health() -> bool:
    """
    检查 CosyVoice 推理服务健康状态。

    Returns:
        服务可用且模型已加载返回 True，否则 False。
    """
    try:
        async with _get_cosyvoice_client() as client:
            response = await client.get(f"{settings.cosyvoice_base_url}/health", timeout=5.0)
            if response.status_code != 200:
                return False
            return response.json().get("status") == "ok"
    except Exception:
        return False


async def _call_cosyvoice_tts(
    text: str,
    speaker_id: str | None,
    ref_audio_path: str | None,
    output_path: Path,
) -> dict:
    """
    调用 CosyVoice 推理服务完成合成（恒定 1.0x 正常语速）。

    推理侧不传 speed：CosyVoice 以 mel 频谱插值实现变速会破坏音色，
    语速调整统一由后端 atempo 变速完成（见 _wav_to_mp3）。

    Args:
        text: 待合成文本。
        speaker_id: 内置音色 ID（内置音色模式）。
        ref_audio_path: 克隆参考音频路径（克隆模式）。
        output_path: 合成 WAV 保存路径。

    Returns:
        推理服务返回的结果字典（含 duration 等字段）。

    Raises:
        RuntimeError: 服务不可用或返回业务错误时抛出。
    """
    payload = {
        "text": text,
        "speaker_id": speaker_id,
        "ref_audio_path": ref_audio_path,
        "output_path": str(output_path),
    }
    try:
        async with _get_cosyvoice_client() as client:
            response = await client.post(
                f"{settings.cosyvoice_base_url}/api/tts", json=payload
            )
    except httpx.TimeoutException as exc:
        # 超时与"不可达"是两回事：长文本分段合成耗时随字数近似线性增长
        # （实测约 0.45 秒/字），需给出可操作的提示而非误导成连接问题
        raise RuntimeError(
            f"CosyVoice 合成超时（上限 {settings.cosyvoice_timeout:.0f} 秒，"
            f"本次文本 {len(text)} 字）：可精简文本或调大 COSYVOICE_TIMEOUT"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"CosyVoice 推理服务不可达: {exc}") from exc

    if response.status_code != 200:
        # 推理服务异常时可能返回非 JSON（空响应体或 HTML 错误页），
        # 必须容错解析，否则会以 JSONDecodeError 掩盖真实失败原因
        detail = ""
        try:
            detail = str(response.json().get("detail", ""))
        except ValueError:
            detail = response.text
        except Exception:
            detail = ""
        detail = (detail or "").strip()
        raise RuntimeError(
            f"CosyVoice 合成失败（HTTP {response.status_code}）: "
            f"{detail[:500] or '推理服务未返回错误详情'}"
        )
    return response.json()


async def synthesize_speech(
    project_id: str,
    speaker_id: str,
    text: str,
    speed: float = 1.0,
    ref_audio_path: str | None = None,
) -> str:
    """
    使用 CosyVoice3 将文本合成为音频。

    内置音色（builtin: 前缀）直接使用预置 speaker；
    克隆音色以 ref_audio_path 指向的 raw_recording.wav 零样本合成。
    推理恒定 1.0x 合成，语速由输出转码阶段的 atempo 变速实现（保音高）；
    参考录音若为遗留的非 WAV 格式，会先自动转码修复。

    Args:
        project_id: 项目 ID，用于保存产物。
        speaker_id: 音色标识（builtin:xxx 或 clone_xxx）。
        text: 待合成文本。
        speed: 语速系数（0.5 ~ 2.0，1.0 为正常）。
        ref_audio_path: 克隆音色参考录音绝对路径（克隆模式必填，
            由路由层解析：优先当前项目，其次音色来源项目）。

    Returns:
        本地保存的 tts.mp3 对外 URL。

    Raises:
        RuntimeError: 合成失败或参考录音缺失时抛出。
    """
    project_dir = get_project_dir(project_id)
    wav_path = project_dir / "_tts_raw.wav"
    mp3_path = get_project_file_path(project_id, "tts.mp3")

    try:
        if speaker_id.startswith(BUILTIN_PREFIX):
            # 内置音色模式：传入预置音色名
            builtin_spk = speaker_id[len(BUILTIN_PREFIX):]
            await _call_cosyvoice_tts(text, builtin_spk, None, wav_path)
        else:
            # 克隆音色模式：以解析出的原始录音为参考
            if not ref_audio_path or not Path(ref_audio_path).exists():
                raise RuntimeError(
                    "克隆参考录音缺失（raw_recording.wav 不存在），请重新完成声音克隆"
                )
            # 兼容本地遗留数据：扩展名为 .wav 但内容实为 webm 等格式时，
            # 就地转码修复，避免推理服务解码失败
            ref_path = await ensure_reference_wav(Path(ref_audio_path))
            await _call_cosyvoice_tts(text, None, str(ref_path), wav_path)

        # 统一转换为 24kHz 单声道 MP3（speed != 1.0 时做保音高变速）
        await asyncio.to_thread(_wav_to_mp3, wav_path, mp3_path, speed)
    finally:
        # 失败路径同样清理临时 WAV：推理服务失败/转码失败时可能已写出
        # 部分内容，长音频可达数十 MB，残留会随失败次数累积
        wav_path.unlink(missing_ok=True)
    return get_public_url(project_id, "tts.mp3")


def get_tts_library_dir() -> Path:
    """
    返回语音库音频文件目录（不存在时自动创建）。

    语音库文件集中存放于上传根目录的 _tts_library/ 子目录，
    通过 /uploads 静态挂载对外提供访问，与各项目目录互不影响。

    Returns:
        语音库目录绝对路径。
    """
    library_dir = settings.upload_root / "_tts_library"
    library_dir.mkdir(parents=True, exist_ok=True)
    return library_dir
