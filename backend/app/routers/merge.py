"""
合并与预览路由

将视频、TTS 音频、BGM 合并为最终视频并提供预览/下载。
"""

import asyncio
import traceback
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Project
from app.services.file_store import get_project_file_path, save_upload_file
from app.services.media_merge import (
    AUDIO_MODE_OVERLAY,
    AUDIO_MODE_REPLACE,
    get_media_duration,
    merge_final_video,
)

router = APIRouter()


@router.post("")
async def merge_media(
    project_id: str = Form(...),
    bgm_volume: float = Form(0.2, ge=0.0, le=1.0),
    fade_in: float = Form(1.0, ge=0.0, le=10.0),
    fade_out: float = Form(2.0, ge=0.0, le=10.0),
    audio_mode: str = Form(AUDIO_MODE_REPLACE),
    force_merge: bool = Form(False),
    bgm: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    """
    合并视频、TTS 音频与可选 BGM。

    若上传了 BGM 文件，则优先使用上传文件；否则尝试使用项目已有的 bgm_path。
    audio_mode 指定音频模式：replace 替换视频原声（默认）、
    overlay 叠加视频原声（视频无原声时自动回退为替换）。
    force_merge 指定音频超长时是否仍强制合并（默认 False：
    TTS 音频时长超过视频时长会先返回 400 提示，避免语音被
    静默截断导致内容丢失；确认知情后可传 True 强制截断合并）。
    音量/淡入淡出参数均有范围约束，非法值直接返回 422。
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    if project.video_status != "ready" or not project.video_path:
        raise HTTPException(status_code=400, detail="视频尚未生成")
    if project.tts_status != "ready" or not project.tts_path:
        raise HTTPException(status_code=400, detail="TTS 音频尚未生成")

    # 防重入：合并进行中拒绝重复提交，避免并发 FFmpeg 写同一 final.mp4
    if project.merge_status == "generating":
        raise HTTPException(status_code=409, detail="合并正在进行中，请等待完成")

    # 音频超长检测：合并以视频时长为准，超出的音频会被截断。
    # 未显式确认（force_merge）前先拦截，避免长配音被静默裁掉大半
    if not force_merge:
        video_file = get_project_file_path(project_id, "video.mp4")
        tts_file = get_project_file_path(project_id, "tts.mp3")
        # ffprobe 为同步阻塞调用，放入线程避免冻结事件循环
        video_duration = await asyncio.to_thread(get_media_duration, video_file)
        tts_duration = await asyncio.to_thread(get_media_duration, tts_file)
        if video_duration > 0 and tts_duration > video_duration + 0.1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"音频时长（{tts_duration:.1f} 秒）超过视频时长（{video_duration:.1f} 秒），"
                    f"直接合并将丢失约 {tts_duration - video_duration:.1f} 秒的语音内容。"
                    f"请回到第 4 步生成更长的视频（时长上限 300 秒），"
                    f"或在「上传视频」中使用等长的本地视频；"
                    f"如确认接受截断，可重新提交并携带 force_merge=true。"
                ),
            )

    # 保存上传的 BGM
    bgm_path: Path | None = None
    if bgm:
        allowed_types = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav"}
        if bgm.content_type not in allowed_types:
            raise HTTPException(status_code=400, detail=f"不支持的 BGM 格式: {bgm.content_type}")
        bgm_bytes = await bgm.read()
        if len(bgm_bytes) > 50 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="BGM 文件过大，限制 50MB")
        bgm_path = save_upload_file(project_id, "bgm.mp3", bgm_bytes)
        project.bgm_path = f"/uploads/{project_id}/bgm.mp3"

    if not bgm_path and project.bgm_path:
        bgm_path = get_project_file_path(project_id, Path(project.bgm_path).name)

    # 校验音频模式，非法值按默认替换处理
    if audio_mode not in (AUDIO_MODE_REPLACE, AUDIO_MODE_OVERLAY):
        audio_mode = AUDIO_MODE_REPLACE

    project.merge_status = "generating"
    await db.commit()

    try:
        # FFmpeg 为同步阻塞长任务，放入线程避免合并期间冻结事件循环
        final_url = await asyncio.to_thread(
            merge_final_video,
            project_id=project_id,
            bgm_path=bgm_path,
            bgm_volume=bgm_volume,
            fade_in=fade_in,
            fade_out=fade_out,
            audio_mode=audio_mode,
        )
        project.merge_status = "ready"
        project.final_video_path = final_url
        await db.commit()
        return {"project_id": project_id, "status": "ready", "final_url": final_url}
    except Exception as exc:
        project.merge_status = "failed"
        project.merge_error = f"{exc}\n{traceback.format_exc()}"
        await db.commit()
        raise HTTPException(status_code=500, detail=f"合并失败: {exc}")


@router.get("/preview/{project_id}")
async def preview_video(project_id: str, db: AsyncSession = Depends(get_db)):
    """流式返回最终合并好的视频文件。"""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    if project.merge_status != "ready" or not project.final_video_path:
        raise HTTPException(status_code=400, detail="最终视频尚未生成")

    final_path = get_project_file_path(project_id, "final.mp4")
    if not final_path.exists():
        raise HTTPException(status_code=404, detail="最终视频文件不存在")

    return FileResponse(
        path=str(final_path),
        media_type="video/mp4",
        filename=f"{project_id}_final.mp4",
    )
