"""
SQLAlchemy 数据模型

Project 表记录每个音视频生成项目的状态与产物路径；
TTSItem 表为语音库条目，保存每次 TTS 合成的产物与元信息。
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, String, Text

from app.database import Base


def _new_uuid() -> str:
    """生成新的唯一标识。"""
    return uuid.uuid4().hex


class Project(Base):
    """项目模型，保存整个音视频流程的状态。"""

    __tablename__ = "projects"

    id = Column(String(32), primary_key=True, default=_new_uuid)
    name = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 语音阶段
    voice_status = Column(String(20), default="pending")  # pending / cloning / ready / failed
    speaker_id = Column(String(255), nullable=True)
    speaker_name = Column(String(255), nullable=True)
    raw_recording_path = Column(String(500), nullable=True)
    voice_error = Column(Text, nullable=True)

    # TTS 阶段
    tts_status = Column(String(20), default="pending")  # pending / generating / ready / failed
    tts_text = Column(Text, nullable=True)
    tts_path = Column(String(500), nullable=True)
    tts_error = Column(Text, nullable=True)

    # 图片阶段
    image_status = Column(String(20), default="pending")
    image_prompt = Column(Text, nullable=True)
    image_path = Column(String(500), nullable=True)
    image_error = Column(Text, nullable=True)

    # 视频阶段
    video_status = Column(String(20), default="pending")  # pending / generating / ready / failed
    video_task_id = Column(String(255), nullable=True)
    video_prompt = Column(Text, nullable=True)
    video_path = Column(String(500), nullable=True)
    video_error = Column(Text, nullable=True)

    # 合并阶段
    merge_status = Column(String(20), default="pending")
    final_video_path = Column(String(500), nullable=True)
    bgm_path = Column(String(500), nullable=True)
    merge_error = Column(Text, nullable=True)


class TTSItem(Base):
    """语音库条目模型，保存每次 TTS 合成的产物与元信息。"""

    __tablename__ = "tts_items"

    id = Column(String(32), primary_key=True, default=_new_uuid)
    # 生成该语音时所属的项目（用于追溯，删除项目不级联删除语音库）
    project_id = Column(String(32), index=True)
    speaker_id = Column(String(255), nullable=True)
    speaker_name = Column(String(255), nullable=True)
    text = Column(Text, nullable=True)
    # 语速系数（1.0 为正常）
    speed = Column(Float, default=1.0)
    # 音频文件名，位于上传根目录的 _tts_library/ 子目录
    file_name = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
