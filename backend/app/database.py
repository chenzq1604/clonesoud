"""
数据库连接与会话管理

使用 SQLAlchemy 2.0 + aiosqlite 提供异步 SQLite 访问。
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from app.config import settings

# 将 sqlite:/// 转换为 sqlite+aiosqlite:///
_db_url = settings.database_url
if _db_url.startswith("sqlite:///"):
    _async_db_url = _db_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
else:
    _async_db_url = _db_url

# 创建异步引擎与会话工厂
engine = create_async_engine(_async_db_url, echo=False, future=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# 模型基类
Base = declarative_base()


async def get_db():
    """FastAPI 依赖：获取异步数据库会话。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
