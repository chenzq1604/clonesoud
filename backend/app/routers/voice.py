"""
语音克隆路由

处理录音上传（本地零样本克隆，即时就绪）、已克隆音色与内置音色列表。
"""

import traceback

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Project
from app.schemas import VoiceOut
from app.services.file_store import get_public_url
from app.services.voice_clone import clone_voice

router = APIRouter()


async def _update_voice_status(
    db: AsyncSession,
    project_id: str,
    status: str,
    speaker_id: str | None = None,
    speaker_name: str | None = None,
    raw_recording_path: str | None = None,
    error: str | None = None,
) -> None:
    """更新数据库中项目的语音阶段状态。"""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        return
    project.voice_status = status
    if speaker_id is not None:
        project.speaker_id = speaker_id
    if speaker_name is not None:
        project.speaker_name = speaker_name
    if raw_recording_path is not None:
        project.raw_recording_path = raw_recording_path
    if error is not None:
        project.voice_error = error
    await db.commit()


async def _run_clone_task(
    project_id: str,
    audio_bytes: bytes,
    speaker_name: str | None,
) -> None:
    """后台执行本地零样本克隆（保存参考录音并生成音色标识）。"""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            await _update_voice_status(db, project_id, "cloning")
            speaker_id = await clone_voice(project_id, audio_bytes, speaker_name)
            # 零样本克隆无训练过程，保存录音后即时就绪
            await _update_voice_status(
                db,
                project_id,
                "ready",
                speaker_id=speaker_id,
                speaker_name=speaker_name,
                raw_recording_path=get_public_url(project_id, "raw_recording.wav"),
            )
        except Exception as exc:
            error_msg = f"{exc}\n{traceback.format_exc()}"
            await _update_voice_status(db, project_id, "failed", error=error_msg)


@router.post("/upload")
async def upload_voice(
    background_tasks: BackgroundTasks,
    project_id: str = Form(...),
    speaker_name: str = Form(""),
    language: int = Form(0),
    audio: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    上传录音完成本地声音克隆。

    接收 multipart 音频文件，校验后保存到本地（统一转 WAV）。
    CosyVoice3 为零样本架构，保存参考录音后音色即时可用。
    """
    # 校验项目存在
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    # 校验文件类型与大小
    allowed_types = {"audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3", "audio/webm"}
    if audio.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail=f"不支持的音频格式: {audio.content_type}")

    audio_bytes = await audio.read()
    if len(audio_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="音频文件过大，限制 20MB")

    # 提交后台任务（服务层会将录音统一转换为 WAV 再保存）
    background_tasks.add_task(
        _run_clone_task, project_id, audio_bytes, speaker_name or None
    )

    return {
        "project_id": project_id,
        "status": "cloning",
        "message": "录音已接收，正在保存克隆音色",
    }


@router.get("/voices", response_model=list[VoiceOut])
async def list_voices(db: AsyncSession = Depends(get_db)):
    """
    查询可用音色列表（内置音色 + 已克隆音色），供 TTS 步骤选择。

    内置音色来自本地 CosyVoice3 模型（推理服务不可用时跳过），
    并附带约 2 秒示例试听地址（片段未生成时为 null）；
    克隆音色来自项目库；同一克隆音色（speaker_id）只保留最新记录。
    克隆音色附带原始录音试听地址（文件已清理的历史音色返回 null，
    仅不可试听，仍可用于合成）。
    """
    from app.services.file_store import get_project_file_path
    from app.services.tts import (
        BUILTIN_PREVIEW_URL_PREFIX,
        BUILTIN_PREFIX,
        get_builtin_preview_path,
        list_builtin_speakers,
    )

    voices: list[VoiceOut] = []

    # 1. 内置音色（本地推理服务提供，预置参考音频清单）
    try:
        builtin_speakers = await list_builtin_speakers()
    except Exception:
        builtin_speakers = []
    for spk in builtin_speakers:
        # 试听片段已预生成时提供约 2 秒示例试听地址
        preview_url = (
            f"{BUILTIN_PREVIEW_URL_PREFIX}{spk['id']}"
            if get_builtin_preview_path(spk["id"]).exists()
            else None
        )
        voices.append(
            VoiceOut(
                project_id=None,
                speaker_id=f"{BUILTIN_PREFIX}{spk['id']}",
                speaker_name=str(spk.get("name", spk["id"])),
                raw_recording_url=preview_url,
                created_at=None,
                is_builtin=True,
            )
        )

    # 2. 已克隆音色（数据库）
    result = await db.execute(
        select(Project)
        .where(Project.voice_status == "ready")
        .order_by(Project.created_at.desc())
    )
    projects = result.scalars().all()

    # 同一 speaker_id 去重：默认保留最新记录；若最新记录的原音文件
    # 已缺失而更早记录仍保留录音，则改用有录音的记录（保证可试听、
    # 可回溯参考音频来源）
    seen_speakers: dict[str, VoiceOut] = {}
    for p in projects:
        if not p.speaker_id:
            continue
        # 选择内置音色时项目也会被标记为 ready 并写入 builtin: 前缀的
        # speaker_id，这里需排除，避免内置音色被当作克隆音色重复列出
        if p.speaker_id.startswith(BUILTIN_PREFIX):
            continue
        raw_url = None
        if get_project_file_path(p.id, "raw_recording.wav").exists():
            raw_url = get_public_url(p.id, "raw_recording.wav")
        prev = seen_speakers.get(p.speaker_id)
        if prev is None or (prev.raw_recording_url is None and raw_url is not None):
            seen_speakers[p.speaker_id] = VoiceOut(
                project_id=p.id,
                speaker_id=p.speaker_id,
                speaker_name=p.speaker_name or "未命名音色",
                raw_recording_url=raw_url,
                created_at=p.created_at,
                is_builtin=False,
            )
    voices.extend(seen_speakers.values())
    return voices


@router.get("/builtin-preview/{speaker_id}")
async def builtin_voice_preview(speaker_id: str):
    """
    返回内置音色的示例试听音频（约 2 秒 WAV）。

    试听片段由 make_builtin_previews.py 预生成（与真实合成效果一致），
    供用户在选择音色前了解声音风格。
    """
    from app.services.tts import get_builtin_preview_path

    # 音色 ID 会拼入文件路径，仅允许安全字符，防路径穿越
    if not speaker_id or any(ch in speaker_id for ch in ("/", "\\", "..")):
        raise HTTPException(status_code=404, detail="试听音频不存在")
    path = get_builtin_preview_path(speaker_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="试听音频不存在")
    return FileResponse(path, media_type="audio/wav", filename=f"{speaker_id}.wav")
