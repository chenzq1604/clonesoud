"""
项目 CRUD 路由

管理音视频生成项目的生命周期与状态查询。
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Project
from app.schemas import ProjectCreate, ProjectOut
from app.services.comfyui_video import (
    clear_comfyui_stale_tasks,
    has_active_generations,
)
from app.services.file_store import remove_project_dir

router = APIRouter()

logger = logging.getLogger(__name__)


async def _clear_stale_tasks_background() -> None:
    """后台清理 ComfyUI 遗留任务（新建项目时触发）。

    独立协程执行，不阻塞建项目响应；ComfyUI 不可达或卡死时仅记日志。
    若本后端其他项目正在生成视频，则队列中的任务并非"遗留"，
    跳过清理以免中断他人任务。
    """
    if has_active_generations():
        logger.info("存在进行中的视频生成任务，跳过新建项目时的 ComfyUI 清场")
        return
    try:
        stale_running, stale_pending = await clear_comfyui_stale_tasks()
        if stale_running or stale_pending:
            logger.warning(
                "新建项目时清理 ComfyUI 遗留任务：中断运行中 %d 个，清空排队 %d 个",
                stale_running, stale_pending,
            )
    except Exception as exc:
        logger.warning("新建项目时清理 ComfyUI 任务失败（不影响项目创建）: %s", exc)


@router.post("", response_model=ProjectOut)
async def create_project(
    data: ProjectCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    创建新项目。

    创建后在后台清理 ComfyUI 遗留任务（中断上次运行残留的旧任务并
    清空队列），保证新项目从干净的 GPU 状态开始；清理不阻塞建项目
    响应，失败（服务未启动或已卡死）也仅记日志——生成视频前仍会
    再次清场兜底。
    """
    project = Project(name=data.name)
    db.add(project)
    await db.commit()
    await db.refresh(project)

    background_tasks.add_task(_clear_stale_tasks_background)
    return project


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """查询项目详情与各阶段状态。"""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.delete("/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """删除项目并清理本地文件。

    先删本地目录再提交 DB 事务：Windows 下项目文件被占用（FFmpeg
    正在写、浏览器正在下载预览）时 rmtree 会抛异常——若 DB 已先提交，
    记录消失导致该目录永远无法再次触达，成为永久孤儿。先删文件可保证
    失败时项目记录仍在，用户可稍后重试删除。
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        remove_project_dir(project_id)
    except OSError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "删除项目文件失败（文件可能正被占用，请关闭正在播放/下载的"
                f"该项目媒体后重试）: {exc}"
            ),
        )
    await db.delete(project)
    await db.commit()
    return {"message": "项目已删除"}
