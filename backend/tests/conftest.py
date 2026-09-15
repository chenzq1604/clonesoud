"""
Pytest 共享 fixtures

在导入应用前将数据库与上传目录切换到独立的测试路径，
确保单元测试不会污染开发/生产数据（projects.db 与 uploads/）。
"""

import os
import subprocess
import time
from pathlib import Path

# 必须在导入 app 之前设置：Settings 在模块导入时实例化并读取环境变量，
# 环境变量优先级高于 .env 文件
os.environ["DATABASE_URL"] = "sqlite:///./test_projects.db"
os.environ["UPLOAD_DIR"] = "../uploads_test"

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.database import Base, engine
from app.main import app

# 边界测试直连的独立后端端口（由 edge_case_backend fixture 启动）
EDGE_BACKEND_PORT = 8377
EDGE_BACKEND_URL = f"http://127.0.0.1:{EDGE_BACKEND_PORT}"

# 边界测试上传录音用的测试音频（16kHz 单声道正弦波）
TEST_VOICE = Path(__file__).resolve().parent / "_test_voice.wav"
if not TEST_VOICE.exists():
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:r=16000",
            "-t", "3", "-ac", "1", "-acodec", "pcm_s16le",
            str(TEST_VOICE),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
async def async_client():
    """
    提供基于 ASGI 的异步 HTTP 客户端。

    每个测试前重建测试库表，保证用例之间数据隔离；
    ASGITransport 不会触发应用 lifespan，因此需要在此手动建表
    （drop_all 对不存在的表是空操作，首个测试前会自动完成初始化）。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(scope="session")
def edge_case_backend():
    """
    为边界测试启动独立后端进程。

    以真实 HTTP 服务方式运行（子进程继承本模块设置的测试库与
    测试上传目录环境变量），测试结束后关闭进程。
    """
    import sys

    backend_dir = Path(__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(EDGE_BACKEND_PORT),
        ],
        cwd=backend_dir,
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 轮询等待服务就绪（最长 30 秒）
    for _ in range(60):
        try:
            if httpx.get(f"{EDGE_BACKEND_URL}/health", timeout=1.0).status_code == 200:
                break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.fail("边界测试后端启动失败")

    yield EDGE_BACKEND_URL

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
