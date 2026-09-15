"""
语音合成路由

使用内置音色或克隆音色将文本合成为音频，支持语速选择；
每次合成自动存入语音库（可查询、选用、删除）。
"""

import shutil
import traceback
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Project, TTSItem
from app.schemas import TTSItemOut, TTSItemUseRequest, TTSRequest
from app.services.file_store import get_project_file_path, get_public_url
from app.services.tts import (
    BUILTIN_PREFIX,
    get_builtin_speaker_name,
    get_tts_library_dir,
    synthesize_speech,
)

router = APIRouter()


def _resolve_ref_audio(project: Project, voice_project: Project | None) -> str | None:
    """
    解析克隆音色的参考录音绝对路径。

    优先使用当前项目保存的录音，其次使用音色来源项目的录音。

    Args:
        project: 当前合成项目。
        voice_project: 该音色来源项目（可能与当前项目相同或为 None）。

    Returns:
        参考录音绝对路径；两处均缺失时返回 None。
    """
    if get_project_file_path(project.id, "raw_recording.wav").exists():
        return str(get_project_file_path(project.id, "raw_recording.wav"))
    if voice_project is not None and voice_project.id != project.id:
        path = get_project_file_path(voice_project.id, "raw_recording.wav")
        if path.exists():
            return str(path)
    return None


async def _find_voice_source_project(db: AsyncSession, speaker_id: str) -> Project | None:
    """
    查找某个克隆音色仍保留参考录音的来源项目。

    选择历史音色的新项目本身没有录音，需回溯到原始克隆项目才能合成。

    Args:
        db: 数据库会话。
        speaker_id: 克隆音色标识。

    Returns:
        优先返回录音存在的项目；均缺失时返回最早的候选项目；无候选返回 None。
    """
    result = await db.execute(
        select(Project)
        .where(Project.speaker_id == speaker_id, Project.voice_status == "ready")
        .order_by(Project.created_at.desc())
    )
    candidates = result.scalars().all()
    if not candidates:
        return None
    return next(
        (
            p for p in candidates
            if get_project_file_path(p.id, "raw_recording.wav").exists()
        ),
        candidates[0],
    )


@router.post("/generate")
async def generate_tts(data: TTSRequest, db: AsyncSession = Depends(get_db)):
    """
    使用内置音色或克隆音色生成 TTS 音频。

    可通过 speaker_id 指定音色列表中的任意一项（内置音色以
    builtin: 前缀标识），未指定时要求项目自身语音阶段状态为 ready。
    """
    result = await db.execute(select(Project).where(Project.id == data.project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    # 确定本次合成使用的音色
    speaker_id = None
    ref_audio_path = None
    if data.speaker_id:
        if data.speaker_id.startswith(BUILTIN_PREFIX):
            # 内置音色：无需数据库校验，直接记录选择
            speaker_id = data.speaker_id
            project.speaker_id = speaker_id
            project.speaker_name = speaker_id[len(BUILTIN_PREFIX):]
            # 选择内置音色后无需克隆，语音阶段视为就绪
            project.voice_status = "ready"
        else:
            # 克隆音色：校验所选音色确为已克隆完成的音色，并同步信息
            # 优先选择仍保存有参考录音的项目作为音色来源：
            # 刚选择历史音色的新项目本身没有录音，需回溯到原始克隆项目
            voice_project = await _find_voice_source_project(db, data.speaker_id)
            if voice_project is None:
                raise HTTPException(status_code=400, detail="所选音色不存在或未就绪")
            speaker_id = data.speaker_id
            project.speaker_id = speaker_id
            project.speaker_name = voice_project.speaker_name
            # 选择历史音色后当前项目视为已就绪，无需重新克隆
            project.voice_status = "ready"
            if voice_project.id != project.id and voice_project.raw_recording_path:
                project.raw_recording_path = voice_project.raw_recording_path
            ref_audio_path = _resolve_ref_audio(project, voice_project)
    elif project.voice_status == "ready" and project.speaker_id:
        speaker_id = project.speaker_id
        if not speaker_id.startswith(BUILTIN_PREFIX):
            # 项目绑定的克隆音色可能来自历史项目（本项目无录音），
            # 需回溯到仍保留录音的源项目解析参考音频
            voice_project = await _find_voice_source_project(db, speaker_id)
            ref_audio_path = _resolve_ref_audio(project, voice_project)
            if not ref_audio_path:
                raise HTTPException(
                    status_code=400,
                    detail="克隆参考录音缺失，请重新完成声音克隆",
                )

    if not speaker_id:
        raise HTTPException(status_code=400, detail="音色尚未准备就绪，请先选择或克隆音色")

    # 防重入：合成进行中拒绝重复提交，避免并发写同一临时 WAV 与产物
    if project.tts_status == "generating":
        raise HTTPException(status_code=409, detail="语音正在合成中，请等待完成")

    project.tts_status = "generating"
    project.tts_text = data.text
    await db.commit()

    try:
        tts_url = await synthesize_speech(
            project_id=data.project_id,
            speaker_id=speaker_id,
            text=data.text,
            speed=data.speed,
            ref_audio_path=ref_audio_path,
        )
        # 同步保存到语音库：复制产物到 _tts_library 并记录元信息
        # 内置音色项目侧仅存 ID，这里解析为清单中的显示名便于展示
        item_speaker_name = project.speaker_name
        if speaker_id.startswith(BUILTIN_PREFIX):
            item_speaker_name = (
                get_builtin_speaker_name(speaker_id[len(BUILTIN_PREFIX):])
                or project.speaker_name
            )
        item = TTSItem(
            project_id=project.id,
            speaker_id=speaker_id,
            speaker_name=item_speaker_name,
            text=data.text,
            speed=data.speed,
            file_name=f"{uuid.uuid4().hex}.mp3",
        )
        shutil.copyfile(
            get_project_file_path(project.id, "tts.mp3"),
            get_tts_library_dir() / item.file_name,
        )
        db.add(item)

        project.tts_status = "ready"
        project.tts_path = tts_url
        # 成功时清空上一次失败留下的错误详情（与视频/选用路径保持一致）
        project.tts_error = None
        await db.commit()
        return {"project_id": data.project_id, "status": "ready", "tts_url": tts_url}
    except Exception as exc:
        project.tts_status = "failed"
        project.tts_error = f"{exc}\n{traceback.format_exc()}"
        await db.commit()
        raise HTTPException(status_code=500, detail=f"TTS 合成失败: {exc}")


def _item_url(item: TTSItem) -> str | None:
    """返回语音库条目的音频访问地址；文件缺失时返回 None。"""
    if (get_tts_library_dir() / item.file_name).exists():
        return f"/uploads/_tts_library/{item.file_name}"
    return None


@router.get("/library", response_model=list[TTSItemOut])
async def list_tts_library(db: AsyncSession = Depends(get_db)):
    """查询语音库（全部生成过的语音，按生成时间倒序）。"""
    result = await db.execute(select(TTSItem).order_by(TTSItem.created_at.desc()))
    items = result.scalars().all()
    return [
        TTSItemOut(
            id=it.id,
            project_id=it.project_id,
            speaker_id=it.speaker_id,
            speaker_name=it.speaker_name,
            text=it.text,
            speed=it.speed if it.speed is not None else 1.0,
            url=_item_url(it),
            created_at=it.created_at,
        )
        for it in items
    ]


@router.post("/library/{item_id}/use")
async def use_tts_item(
    item_id: str,
    data: TTSItemUseRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    选用语音库中的音频作为目标项目的合成语音。

    将音频复制为目标项目的 tts.mp3 并置 TTS 阶段为 ready，
    供第 5 步合并使用；不改变项目的音色绑定状态。
    """
    item_result = await db.execute(select(TTSItem).where(TTSItem.id == item_id))
    item = item_result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="语音库条目不存在")

    proj_result = await db.execute(select(Project).where(Project.id == data.project_id))
    project = proj_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    src_path = get_tts_library_dir() / item.file_name
    if not src_path.exists():
        raise HTTPException(status_code=404, detail="该条目的音频文件已缺失")

    shutil.copyfile(src_path, get_project_file_path(project.id, "tts.mp3"))
    project.tts_status = "ready"
    project.tts_path = get_public_url(project.id, "tts.mp3")
    project.tts_text = item.text
    project.tts_error = None
    await db.commit()

    return {
        "project_id": project.id,
        "status": "ready",
        "tts_path": project.tts_path,
        "message": "已选用语音库音频",
    }


@router.delete("/library/{item_id}")
async def delete_tts_item(item_id: str, db: AsyncSession = Depends(get_db)):
    """删除语音库条目及其音频文件。"""
    result = await db.execute(select(TTSItem).where(TTSItem.id == item_id))
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="语音库条目不存在")

    await db.delete(item)
    await db.commit()
    # 已选用到项目中的 tts.mp3 副本不受影响
    (get_tts_library_dir() / item.file_name).unlink(missing_ok=True)
    return {"message": "语音库条目已删除"}
