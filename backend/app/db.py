"""Database engine and session factory.

Django opens a connection per request behind the scenes. With SQLAlchemy we
create one engine (which owns a connection pool) for the process, and hand out
short-lived AsyncSession objects per request via the `get_db` dependency in
`app/deps.py`.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

engine = create_async_engine(
    str(get_settings().database_url),
    pool_pre_ping=True,  # transparently replace connections the DB has dropped
)

# expire_on_commit=False keeps loaded attributes usable after commit, which
# matters in async code where lazy-reloading them would need another await.
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
