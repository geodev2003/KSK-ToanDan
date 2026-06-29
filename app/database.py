import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

# postgresql+asyncpg://user:pass@host:port/dbname
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://ksk:ksk_secret_2026@localhost:5432/ksk",
)

if DATABASE_URL.startswith("sqlite"):
    # phương án nhẹ: SQLite (1 file, không cần cài Postgres)
    engine = create_async_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})
else:
    # PostgreSQL: pool đủ cho vài chục người nhập đồng thời
    engine = create_async_engine(
        DATABASE_URL, echo=False,
        pool_size=20, max_overflow=20, pool_pre_ping=True,
    )

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
