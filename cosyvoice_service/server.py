"""
CosyVoice3 本地语音合成服务

以独立进程运行（conda 环境 cosyvoice），供主后端通过 HTTP 调用。

端点：
- GET  /health        : 健康检查（含模型加载状态）
- GET  /api/speakers  : 返回内置音色列表（预置参考音频清单）
- POST /api/tts       : 语音合成，支持内置音色（speaker_id）与
                        克隆音色（ref_audio_path 参考音频）两种模式

说明：CosyVoice 模型推理非线程安全，使用全局锁串行化请求；
两种模式均采用 inference_cross_lingual（无需参考音频的文字转录）。
长文本会先按前端规则切分为短片段逐段合成，再插入静音拼接，
避免整段送入导致的提前停止、无停顿与后段退化（见 _split_text_for_tts）。
内置音色：Fun-CosyVoice3-0.5B 为零样本基座模型、不带预置说话人
（spk2info），内置音色以若干预置参考音频实现，位于 builtin_voices/
目录，由 manifest.json 描述（可用 make_builtin_voices.py 生成）。
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# 必须在导入 librosa/numba 之前设置：numba 缓存目录校验会在导入时执行
# tempfile.TemporaryFile（caching.ensure_cache_path），该操作在受限沙箱
# 环境下会死锁；禁用 JIT 可完全绕过（librosa 退化为纯 Python，仅影响
# mel 谱相关辅助计算，GPU 主推理不受影响）。
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")

# 将 CosyVoice 仓库与其 Matcha-TTS 子模块加入模块搜索路径
REPO_ROOT = Path(__file__).resolve().parent.parent / "third_party_CosyVoice"
sys.path.insert(0, str(REPO_ROOT))
sys.path.append(str(REPO_ROOT / "third_party" / "Matcha-TTS"))

import torch
import torchaudio
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("cosyvoice_service")

# CosyVoice3 系列模型要求合成文本携带 BlankEN 前缀（见官方 example.py）
BLANK_PREFIX = "You are a helpful assistant.<|endofprompt|>"

# 长文本分段合成时，片段之间插入的静音时长（秒），用于形成自然停顿
SEGMENT_SILENCE_SECONDS = 0.3

# 内置音色清单与音频目录（预置参考音频，见模块 docstring）
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin_voices"

app = FastAPI(title="CosyVoice3 本地推理服务")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    """
    未捕获异常统一以 JSON 返回错误详情。

    框架默认的 500 响应正文为空，主后端解析时只能得到
    JSONDecodeError，无法定位真实原因（如参考音频格式不合法）。
    """
    logger.exception("推理服务未捕获异常: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}"},
    )

# 全局锁：模型推理非线程安全，串行化并发请求
_infer_lock = threading.Lock()
_model = None


def _load_builtin_manifest() -> list[dict]:
    """读取内置音色清单，返回 [{'id', 'name', 'file'}, ...]。"""
    manifest_path = BUILTIN_DIR / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        voices = [
            v for v in data.get("voices", [])
            if v.get("id") and v.get("name") and (BUILTIN_DIR / v.get("file", "")).exists()
        ]
        return voices
    except Exception as exc:
        logger.warning("读取内置音色清单失败: %s", exc)
        return []


def _builtin_voice_path(speaker_id: str) -> Optional[Path]:
    """按音色 ID 查找预置参考音频路径，不存在返回 None。"""
    for v in _load_builtin_manifest():
        if v["id"] == speaker_id:
            return BUILTIN_DIR / v["file"]
    return None


class TTSRequest(BaseModel):
    """TTS 合成请求体。"""

    text: str = Field(..., min_length=1, description="待合成文本")
    speaker_id: Optional[str] = Field(None, description="内置音色 ID（与 ref_audio_path 二选一）")
    ref_audio_path: Optional[str] = Field(
        None, description="克隆模式参考音频路径（16kHz WAV，与 speaker_id 二选一）"
    )
    speed: float = Field(1.0, ge=0.5, le=2.0, description="语速系数")
    output_path: str = Field(..., description="合成结果 WAV 保存路径")


def _load_model(model_dir: str) -> None:
    """启动时加载 CosyVoice3 模型到显存。"""
    global _model
    from cosyvoice.cli.cosyvoice import CosyVoice3

    logger.info("正在加载 CosyVoice3 模型: %s", model_dir)
    start = time.time()
    _model = CosyVoice3(model_dir, load_trt=False, fp16=True)
    logger.info("模型加载完成，耗时 %.1f 秒，采样率 %d", time.time() - start, _model.sample_rate)


@app.on_event("startup")
async def startup() -> None:
    """FastAPI 启动钩子：加载模型。"""
    if _model is None:
        _load_model(_MODEL_DIR)


@app.get("/health")
async def health() -> dict:
    """健康检查：返回模型加载状态。"""
    return {
        "status": "ok" if _model is not None else "loading",
        "device": "cuda" if torch.cuda.is_available() else "cpu",
    }


@app.get("/api/speakers")
async def list_speakers() -> dict:
    """返回内置音色列表（预置参考音频清单）。"""
    speakers = [
        {"id": v["id"], "name": v["name"]} for v in _load_builtin_manifest()
    ]
    return {"speakers": speakers}


def _resolve_ref_path(req: TTSRequest) -> str:
    """
    解析合成用参考音频路径。

    内置音色模式返回预置参考音频路径；克隆模式返回用户录音路径。
    本版 CosyVoice 的 inference_cross_lingual 接受音频文件路径
    （内部 load_wav 自行重采样），不能传入 tensor。

    Args:
        req: TTS 合成请求。

    Returns:
        参考音频文件的绝对路径字符串。

    Raises:
        HTTPException: 音色未知 / 两个参数均缺失 / 文件不存在。
    """
    if req.speaker_id:
        preset_path = _builtin_voice_path(req.speaker_id)
        if preset_path is None:
            raise HTTPException(
                status_code=400, detail=f"未知内置音色: {req.speaker_id}"
            )
        ref_path = str(preset_path)
    elif req.ref_audio_path:
        ref_path = req.ref_audio_path
    else:
        raise HTTPException(status_code=400, detail="speaker_id 与 ref_audio_path 必须提供其一")

    if not os.path.exists(ref_path):
        raise HTTPException(status_code=400, detail=f"参考音频不存在: {ref_path}")
    return ref_path


def _split_text_for_tts(text: str) -> list[str]:
    """
    按 CosyVoice 前端的规则把长文本切分为若干短片段。

    为什么要先切分：CosyVoice3 的合成文本必须携带 BLANK_PREFIX
    （含 <|endofprompt|>），而 frontend.text_normalize 一见到 <| 与 |>
    就会跳过前端处理并"整段返回"（见 cli/frontend.py 的 ssml 判断），
    导致切分被完全禁用。整篇长文于是作为单次生成送入 LLM：
    模型在超长上下文下会提前"想收尾"，却因 min_len（文本 token 数 ×2）
    压制 EOS 而被迫继续吐 token，结果是开头对不上原文、中间没有停顿、
    后段退化成听不清的内容（实测 964 字仅产出 45 秒，约为应有长度的 1/4）。

    因此必须用**不带前缀的原文**先切分，再逐段拼接前缀合成。

    Args:
        text: 用户输入的原始文本（不含 BLANK_PREFIX）。

    Returns:
        切分后的文本片段列表；切分失败或结果为空时回退为整段。
    """
    try:
        segments = _model.frontend.text_normalize(text, split=True)
    except Exception as exc:
        logger.warning("文本切分失败，回退整段合成: %s", exc)
        return [text]
    segments = [s.strip() for s in segments if s and s.strip()]
    return segments or [text]


def _synthesize_segment(text: str, ref_path: str, speed: float) -> torch.Tensor:
    """
    合成单个文本片段（内部拼接 CosyVoice3 所需的 BLANK_PREFIX）。

    Args:
        text: 单个文本片段。
        ref_path: 参考音频路径。
        speed: 语速系数。

    Returns:
        该片段的音频张量。

    Raises:
        HTTPException: 片段合成结果为空。
    """
    chunks = []
    for out in _model.inference_cross_lingual(
        BLANK_PREFIX + text, ref_path, stream=False, speed=speed
    ):
        chunks.append(out["tts_speech"])
    if not chunks:
        raise HTTPException(status_code=500, detail="合成结果为空")
    return torch.cat(chunks, dim=1)


def _synthesize(req: TTSRequest) -> dict:
    """
    执行同步合成（调用方需持有 _infer_lock）。

    长文本先切分为短片段逐段合成，再在片段之间插入静音后拼接，
    以获得完整内容、正确开头与自然停顿。
    """
    ref_path = _resolve_ref_path(req)
    segments = _split_text_for_tts(req.text)
    logger.info("文本切分: 原文 %d 字 -> %d 段", len(req.text), len(segments))

    speeches = [
        _synthesize_segment(seg, ref_path, req.speed) for seg in segments
    ]

    # 段间插入静音形成自然停顿；单段时不插（行为与旧版一致）
    parts: list[torch.Tensor] = []
    for i, speech in enumerate(speeches):
        if i > 0:
            parts.append(
                torch.zeros(
                    1,
                    int(_model.sample_rate * SEGMENT_SILENCE_SECONDS),
                    dtype=speech.dtype,
                    device=speech.device,
                )
            )
        parts.append(speech)
    merged = torch.cat(parts, dim=1)

    output_path = Path(req.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(output_path), merged, _model.sample_rate)
    duration = merged.shape[1] / _model.sample_rate
    logger.info(
        "合成完成: %s 时长 %.1fs 段数 %d 模式=%s",
        output_path.name, duration, len(segments),
        "内置" if req.speaker_id else "克隆",
    )
    return {
        "output_path": str(output_path),
        "sample_rate": _model.sample_rate,
        "duration": round(duration, 2),
        "segments": len(segments),
    }


@app.post("/api/tts")
async def synthesize(req: TTSRequest) -> dict:
    """语音合成端点（线程池中执行并加锁串行）。

    锁的获取必须放入工作线程：threading.Lock.acquire() 是同步阻塞调用，
    若在事件循环线程上直接 with _infer_lock，第二个请求会在等锁期间
    卡死事件循环，导致持锁请求的完成回调永远无法调度，形成死锁。
    """
    if _model is None:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")
    await asyncio.to_thread(_infer_lock.acquire)
    try:
        return await asyncio.to_thread(_synthesize, req)
    finally:
        _infer_lock.release()


_MODEL_DIR = str(REPO_ROOT / "pretrained_models" / "Fun-CosyVoice3-0.5B-2512")


def main() -> None:
    """解析命令行参数并启动 uvicorn 服务。"""
    global _MODEL_DIR
    parser = argparse.ArgumentParser(description="CosyVoice3 本地推理服务")
    parser.add_argument("--model_dir", default=_MODEL_DIR, help="模型目录路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=9880, help="监听端口")
    args = parser.parse_args()
    _MODEL_DIR = args.model_dir

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
