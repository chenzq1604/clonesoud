"""
FastAPI 应用入口

负责应用初始化、数据库表创建、CORS、静态文件挂载与路由注册。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, engine, Base
from app.models import Project
from app.routers import config, image, merge, projects, tts, video, voice


async def _recover_orphan_video_tasks() -> int:
    """
    启动时恢复遗留的"生成中"视频任务。

    后端重启会杀死正在执行的生成协程，但项目状态仍停留在 generating，
    前端会永远显示"生成中"且无任何进度。此处将其标记为失败并附
    明确提示（重新提交即可断点续传，已完成分段不会重做）。

    Returns:
        恢复（标记失败）的项目数。
    """
    recovered = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Project).where(Project.video_status == "generating"))
        for project in result.scalars():
            project.video_status = "failed"
            project.video_task_id = None
            project.video_error = (
                "后端服务重启导致视频生成任务中断。"
                "请重新点击「生成视频」继续：参数不变时将自动断点续传，"
                "已完成的分段不会重新生成。"
            )
            recovered += 1
        if recovered:
            await db.commit()
    return recovered


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时创建数据库表并恢复遗留任务状态。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    recovered = await _recover_orphan_video_tasks()
    if recovered:
        print(f"[startup] 已恢复 {recovered} 个因重启中断的视频任务（标记为失败，可断点续传）")
    yield
    await engine.dispose()


# 确保上传根目录存在
settings.upload_root.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="音视频克隆生成系统",
    description="基于 CosyVoice3 本地语音 / 火山方舟文生图 / ComfyUI 本地视频的音视频生成 MVP",
    version="0.2.0",
    lifespan=lifespan,
)

# 配置跨域
origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(projects.router, prefix="/api/projects", tags=["项目"])
app.include_router(config.router, prefix="/api/config", tags=["配置"])
app.include_router(voice.router, prefix="/api/voice", tags=["语音复刻"])
app.include_router(tts.router, prefix="/api/tts", tags=["语音合成"])
app.include_router(image.router, prefix="/api/image", tags=["文生图"])
app.include_router(video.router, prefix="/api/video", tags=["图生视频"])
app.include_router(merge.router, prefix="/api/merge", tags=["合并预览"])

# 挂载产物目录为静态文件服务，便于前端预览/下载
app.mount("/uploads", StaticFiles(directory=str(settings.upload_root)), name="uploads")


@app.get("/health")
async def health_check():
    """健康检查接口。"""
    return {"status": "ok"}
