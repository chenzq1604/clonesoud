"""
视频路由

支持三种视频来源：
- 文生视频 / 图生视频：通过本地 ComfyUI（Wan 2.2 5B TI2V）生成；
- 上传视频：将本地已生成好的视频标准化为 video.mp4 后直接使用。

生成分段进度以 JSON 形式写入 video_task_id（前端解析展示）：
{"stage": "generating"/"concat", "done": N, "total": M,
 "seg_progress": 0~1, "resumed": K}
"""

import asyncio
import json
import traceback
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Project
from app.schemas import VideoGenerateRequest
from app.services.comfyui_video import generate_video_with_comfyui, normalize_uploaded_video
from app.services.file_store import (
    get_project_file_path,
    safe_filename,
    save_upload_file,
)

router = APIRouter()

ALLOWED_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/webm"}
ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".webm", ".mkv"}
MAX_VIDEO_SIZE = 200 * 1024 * 1024


async def _update_video_status(
    db: AsyncSession,
    project_id: str,
    status: str,
    video_path: str | None = None,
    error: str | None = None,
    task_id: str | None = None,
) -> None:
    """更新项目视频阶段状态。

    task_id 为 JSON 形式的分段进度（含段内进度与阶段）时写入 video_task_id，
    供前端在 generating 状态下展示；其余情况清空该字段。
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        return
    project.video_status = status
    project.video_task_id = task_id
    if video_path is not None:
        project.video_path = video_path
    if error is not None:
        project.video_error = error
    await db.commit()


def _resolve_image_local_path(project_id: str, image_url: str) -> Path:
    """
    将项目存储的图片 URL（/uploads/<id>/<file>）解析为本地文件路径。

    Args:
        project_id: 项目 ID。
        image_url: 项目内图片的对外 URL。

    Returns:
        图片的本地绝对路径。

    Raises:
        FileNotFoundError: 本地文件不存在。
    """
    filename = Path(image_url.replace("/uploads/", "")).name
    image_path = get_project_file_path(project_id, filename)
    if not image_path.exists():
        # 若 URL 指向的文件名与实际不符，优先尝试 image.png
        fallback = get_project_file_path(project_id, "image.png")
        if fallback.exists():
            return fallback
        raise FileNotFoundError(f"首帧图片不存在: {image_url}")
    return image_path


async def _run_video_task(
    project_id: str,
    prompt: str,
    image_path: Path | None,
    width: int,
    height: int,
    fps: int,
    duration: float,
) -> None:
    """
    后台执行 ComfyUI 视频生成任务（提交工作流 → 轮询 → 下载产物）。

    Args:
        project_id: 项目 ID。
        prompt: 视频内容描述。
        image_path: 首帧图片本地路径；None 表示文生视频。
        width: 视频宽度（16 的倍数）。
        height: 视频高度（16 的倍数）。
        fps: 帧率。
        duration: 期望时长（秒）。
    """
    from app.database import AsyncSessionLocal

    async def _report_progress(state: dict) -> None:
        """分段/段内/拼接进度实时写入 video_task_id（JSON）供前端展示。"""
        task_id = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        async with AsyncSessionLocal() as db:
            await _update_video_status(db, project_id, "generating", task_id=task_id)

    async with AsyncSessionLocal() as db:
        try:
            await _update_video_status(db, project_id, "generating", task_id=None)
            video_url = await generate_video_with_comfyui(
                project_id, prompt, image_path,
                width=width, height=height, fps=fps, duration=duration,
                progress_cb=_report_progress,
            )
            await _update_video_status(db, project_id, "ready", video_path=video_url)
        except Exception as exc:
            error_msg = f"{exc}\n{traceback.format_exc()}"
            # 已完成的分段会保留在项目内，提示用户重新生成可断点续传
            from app.services.comfyui_video import SEGMENTS_STATE_FILE
            from app.services.file_store import get_project_file_path
            state_file = get_project_file_path(project_id, SEGMENTS_STATE_FILE)
            if state_file.exists():
                error_msg += (
                    "\n提示：已完成的分段已保留，保持相同参数重新点击「生成视频」"
                    "将自动从失败的分段继续，无需从头生成。"
                )
            await _update_video_status(db, project_id, "failed", error=error_msg)


@router.post("/generate")
async def generate_video(
    background_tasks: BackgroundTasks,
    data: VideoGenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    """提交视频生成异步任务（本地 ComfyUI 文生视频 / 图生视频）。"""
    result = await db.execute(select(Project).where(Project.id == data.project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    # 宽高必须为 16 的倍数（Wan VAE 下采样因子约束）
    if data.width % 16 != 0 or data.height % 16 != 0:
        raise HTTPException(
            status_code=400,
            detail=f"宽高必须为 16 的倍数（当前 {data.width}x{data.height}）",
        )

    is_image_mode = data.mode == "image_to_video"
    if is_image_mode and not project.image_path:
        raise HTTPException(status_code=400, detail="请先生成并选择图片")

    image_path = None
    if is_image_mode:
        try:
            image_path = _resolve_image_local_path(data.project_id, project.image_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=400, detail=f"首帧图片文件缺失: {exc}")

    project.video_status = "generating"
    project.video_prompt = data.prompt
    project.video_path = None
    project.video_error = None
    project.video_task_id = None
    await db.commit()

    background_tasks.add_task(
        _run_video_task,
        data.project_id,
        data.prompt,
        image_path,
        data.width,
        data.height,
        data.fps,
        data.duration,
    )

    return {
        "project_id": data.project_id,
        "status": "generating",
        "message": "视频生成任务已提交（本地 ComfyUI Wan 2.2）",
    }


@router.post("/upload")
async def upload_video(
    project_id: str = Form(...),
    video: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    上传本地已有的视频作为项目视频产物。

    在本地 GPU 推理耗时较长或不希望重新生成时，用户可上传本地已生成好的视频，
    校验格式与大小后落盘，并标准化为 video.mp4，将项目视频状态置为 ready，
    以便直接进入第 5 步合并。
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    filename = safe_filename(video.filename or "")
    suffix = Path(filename).suffix.lower()
    if video.content_type not in ALLOWED_VIDEO_TYPES and suffix not in ALLOWED_VIDEO_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的视频格式: {video.content_type or suffix or '未知'}",
        )

    data = await video.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传的视频文件为空")
    if len(data) > MAX_VIDEO_SIZE:
        raise HTTPException(status_code=400, detail="视频文件过大，限制 200MB")

    raw_name = f"upload_raw{suffix or '.mp4'}"
    raw_path = save_upload_file(project_id, raw_name, data)
    try:
        # 转码/封装为阻塞型 FFmpeg 调用，放入线程池避免阻塞事件循环
        local_url = await asyncio.to_thread(normalize_uploaded_video, project_id, raw_path)
    except Exception as exc:
        project.video_status = "failed"
        project.video_error = f"本地视频处理失败: {exc}"
        await db.commit()
        raise HTTPException(status_code=500, detail=f"本地视频处理失败: {exc}")
    finally:
        if raw_path.exists() and raw_path.name != "video.mp4":
            raw_path.unlink(missing_ok=True)

    project.video_status = "ready"
    project.video_path = local_url
    project.video_task_id = None
    project.video_error = None
    await db.commit()

    return {
        "project_id": project_id,
        "status": "ready",
        "video_path": local_url,
        "message": "本地视频上传成功",
    }
