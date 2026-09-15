"""
应用配置模块

通过 pydantic-settings 从环境变量 / .env 文件读取敏感配置与运行参数，
确保 API Key、代理等配置不会硬编码到源码中。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置对象。"""

    # 火山引擎方舟 API Key（文生图）
    ark_api_key: str = ""

    # 后端监听配置
    app_host: str = "127.0.0.1"
    app_port: int = 8000

    # 本地代理（用于访问火山引擎海外节点）
    http_proxy: str = "http://127.0.0.1:7890"

    # 文件上传根目录（相对路径基于 backend 目录）
    upload_dir: str = "../uploads"

    # 内置音色预置资源目录（参考音频与试听片段，相对路径基于 backend 目录）
    builtin_voices_dir: str = "../cosyvoice_service/builtin_voices"

    # SQLite 数据库地址
    database_url: str = "sqlite:///./projects.db"

    # 前端跨域来源（开发环境）
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # 火山引擎方舟模型 ID（文生图）
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/plan/v3"
    image_model: str = "doubao-seedream-5.0-lite"

    # CosyVoice3 本地推理服务地址（声音克隆与语音合成）
    cosyvoice_base_url: str = "http://127.0.0.1:9880"
    # 单次合成请求超时（秒）；长文本按前端规则切分后逐段合成，
    # 耗时与文本长度近似线性（实测约 0.45 秒/字），故需留足余量
    cosyvoice_timeout: float = 1800.0

    # 本地 ComfyUI 服务地址（Wan 2.2 5B 文生视频 / 图生视频）
    comfyui_base_url: str = "http://127.0.0.1:8188"
    # 单次视频生成任务超时（秒）；121 帧本地 GPU 推理约需数分钟，留足余量
    comfyui_timeout: float = 1800.0

    # 演示模式：当外部 API 不可用或需要快速跑通流程时，
    # 文生图返回本地占位图（语音已全部本地化，不受此开关影响）
    demo_mode: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def upload_root(self) -> Path:
        """返回解析后的上传目录绝对路径。"""
        path = Path(self.upload_dir)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        return path.resolve()

    @property
    def builtin_voices_root(self) -> Path:
        """返回解析后的内置音色资源目录绝对路径。"""
        path = Path(self.builtin_voices_dir)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        return path.resolve()


# 全局单例，在应用启动时实例化
settings = Settings()
