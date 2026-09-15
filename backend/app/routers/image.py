"""
文生图路由

根据提示词生成一组图片，并支持选择其中一张作为后续视频首帧。
"""

import re
import traceback

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Project
from app.schemas import ImageGenerateRequest
from app.services.file_store import get_project_dir
from app.services.image_gen import generate_image, select_image

router = APIRouter()


class ImageSelectRequest(BaseModel):
    """图片选择请求。"""

    project_id: str
    selected_index: int = Field(..., ge=0, description="选中的图片索引")


@router.post("/generate")
async def generate_image_route(data: ImageGenerateRequest, db: AsyncSession = Depends(get_db)):
    """根据提示词生成一组图片并保存到本地。"""
    result = await db.execute(select(Project).where(Project.id == data.project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    # 防重入：生成进行中拒绝重复提交，避免候选图文件互相覆盖
    if project.image_status == "generating":
        raise HTTPException(status_code=409, detail="图片正在生成中，请等待完成")

    project.image_status = "generating"
    project.image_prompt = data.prompt
    await db.commit()

    try:
        urls = await generate_image(
            project_id=data.project_id,
            prompt=data.prompt,
            size=data.size,
            count=data.count,
        )
        project.image_status = "ready"
        project.image_path = urls[0] if urls else None
        await db.commit()
        return {"project_id": data.project_id, "status": "ready", "image_urls": urls}
    except Exception as exc:
        project.image_status = "failed"
        project.image_error = f"{exc}\n{traceback.format_exc()}"
        await db.commit()
        raise HTTPException(status_code=500, detail=f"文生图失败: {exc}")


@router.post("/select")
async def select_image_route(data: ImageSelectRequest, db: AsyncSession = Depends(get_db)):
    """从已生成的图片中选择一张作为视频首帧。"""
    result = await db.execute(select(Project).where(Project.id == data.project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    # 动态扫描项目目录下已生成的图片文件（按编号数字序，
    # 避免 image_10.png 在字符串排序下跑到 image_2.png 前面）
    project_dir = get_project_dir(data.project_id)

    def _numeric_key(path):
        m = re.search(r"image_(\d+)\.png$", path.name)
        return int(m.group(1)) if m else 0

    image_files = sorted(project_dir.glob("image_*.png"), key=_numeric_key)
    urls = [f"/uploads/{data.project_id}/{f.name}" for f in image_files]
    try:
        selected_url = await select_image(data.project_id, urls, data.selected_index)
        project.image_path = selected_url
        await db.commit()
        return {"project_id": data.project_id, "selected_url": selected_url}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"选择图片失败: {exc}")
