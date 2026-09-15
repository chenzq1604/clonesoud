"""
Pydantic 请求/响应模型

用于 FastAPI 参数校验与序列化。
"""

from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer


def _to_utc_iso(value: datetime) -> str:
    """
    将数据库中的无时区 UTC 时间标注为 UTC 并输出 ISO 8601 字符串。

    数据库统一以 datetime.utcnow() 存储（无时区标记），直接序列化会得到
    "2026-09-12T10:14:06" 这类字符串；前端 new Date() 会把它当作浏览器本地
    时间解析，导致东八区显示比实际早 8 小时。补上 +00:00 时区标记后，
    前端会自动换算为浏览器本地时区（北京时间）。

    Args:
        value: 数据库取出的时间（通常为无时区的 UTC）。

    Returns:
        带 UTC 时区标记的 ISO 8601 字符串。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# 序列化时自动补 UTC 时区标记的时间类型（仅影响 JSON 输出，不改变入库值）
UTCDatetime = Annotated[
    datetime,
    PlainSerializer(_to_utc_iso, return_type=str, when_used="json"),
]


class ProjectCreate(BaseModel):
    """创建项目请求。"""

    name: Optional[str] = Field(None, description="项目名称")


class ProjectOut(BaseModel):
    """项目详情响应。"""

    id: str
    name: Optional[str]
    created_at: UTCDatetime
    updated_at: UTCDatetime

    voice_status: str
    speaker_id: Optional[str]
    speaker_name: Optional[str]
    raw_recording_path: Optional[str]
    voice_error: Optional[str]

    tts_status: str
    tts_text: Optional[str]
    tts_path: Optional[str]
    tts_error: Optional[str]

    image_status: str
    image_prompt: Optional[str]
    image_path: Optional[str]
    image_error: Optional[str]

    video_status: str
    video_task_id: Optional[str]
    video_prompt: Optional[str]
    video_path: Optional[str]
    video_error: Optional[str]

    merge_status: str
    final_video_path: Optional[str]
    bgm_path: Optional[str]
    merge_error: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class TTSRequest(BaseModel):
    """TTS 生成请求。"""

    project_id: str
    text: str = Field(..., min_length=1, max_length=5000, description="待合成文本")
    speaker_id: Optional[str] = Field(
        None, description="指定使用的克隆音色 ID；留空则使用项目绑定的音色"
    )
    speed: float = Field(1.0, ge=0.5, le=2.0, description="语速系数（1.0 为正常）")


class VoiceOut(BaseModel):
    """可用音色列表项（内置音色或克隆音色）。"""

    project_id: Optional[str] = Field(
        None, description="克隆音色来源项目 ID；内置音色为 None"
    )
    speaker_id: str = Field(..., description="音色标识；内置音色以 builtin: 前缀标识")
    speaker_name: str
    raw_recording_url: Optional[str] = Field(
        None,
        description=(
            "试听地址：克隆音色为原始录音；内置音色为约 2 秒示例语音；"
            "缺失或未生成为 null"
        ),
    )
    created_at: Optional[UTCDatetime] = Field(
        None, description="克隆时间（UTC，带时区标记）；内置音色为 None"
    )
    is_builtin: bool = Field(False, description="是否为模型内置音色")


class ImageGenerateRequest(BaseModel):
    """文生图请求。"""

    project_id: str
    prompt: str = Field(..., min_length=1, max_length=4000, description="图片提示词")
    size: str = Field("2K", description="图片尺寸")
    count: int = Field(6, ge=1, le=6, description="候选图片数量（并发生成）")


class TTSItemOut(BaseModel):
    """语音库条目响应。"""

    id: str
    project_id: Optional[str] = Field(None, description="生成时所属项目 ID")
    speaker_id: Optional[str] = None
    speaker_name: Optional[str] = None
    text: Optional[str] = None
    speed: float = 1.0
    url: Optional[str] = Field(None, description="音频试听地址；文件缺失时为 null")
    created_at: Optional[UTCDatetime] = None

    model_config = ConfigDict(from_attributes=True)


class TTSItemUseRequest(BaseModel):
    """从语音库选用音频的请求。"""

    project_id: str = Field(..., description="选用该音频的目标项目")


class VideoGenerateRequest(BaseModel):
    """视频生成请求（本地 ComfyUI 文生视频 / 图生视频共用）。"""

    project_id: str
    prompt: str = Field(..., min_length=1, max_length=4000, description="视频内容描述")
    mode: str = Field(
        "text_to_video",
        pattern=r"^(text_to_video|image_to_video)$",
        description="生成模式：text_to_video=文生视频（默认），image_to_video=图生视频",
    )
    width: int = Field(1280, ge=448, le=1280, description="视频宽度（16 的倍数，默认 1280）")
    height: int = Field(704, ge=448, le=1280, description="视频高度（16 的倍数，默认 704）")
    fps: int = Field(24, ge=8, le=30, description="帧率（默认 24）")
    duration: float = Field(
        5.0, ge=1.0, le=300.0,
        description="期望总时长（秒，默认 5，最长 300=5 分钟）；超出单段预算自动分段生成",
    )


class MergeRequest(BaseModel):
    """合并视频请求。"""

    project_id: str
    bgm_volume: float = Field(0.2, ge=0.0, le=1.0, description="BGM 音量比例")
    fade_in: float = Field(1.0, ge=0.0, le=10.0, description="音频淡入秒数")
    fade_out: float = Field(2.0, ge=0.0, le=10.0, description="音频淡出秒数")


class TaskStatusOut(BaseModel):
    """异步任务状态响应。"""

    status: str
    message: Optional[str] = None
    result_url: Optional[str] = None


class ConfigUpdate(BaseModel):
    """配置更新请求。

    所有字段可选；未提供的字段保持不变。
    ark_api_key 为敏感项：传空字符串表示保持当前值不变（用于只改其他
    配置的场景），传非空字符串则覆盖。
    """

    ark_api_key: Optional[str] = Field(None, description="火山引擎 API Key；空或省略 = 保持不变")
    image_model: Optional[str] = Field(None, min_length=1, max_length=100, description="文生图模型 ID")
    http_proxy: Optional[str] = Field(None, max_length=200, description="本地代理地址")
    comfyui_base_url: Optional[str] = Field(None, max_length=200, description="ComfyUI 服务地址")
    comfyui_timeout: Optional[float] = Field(None, ge=10, le=7200, description="视频生成超时（秒）")
    cosyvoice_base_url: Optional[str] = Field(None, max_length=200, description="CosyVoice 服务地址")
    cosyvoice_timeout: Optional[float] = Field(None, ge=10, le=7200, description="语音合成超时（秒）")
    demo_mode: Optional[bool] = Field(None, description="演示模式（文生图返回占位图）")
