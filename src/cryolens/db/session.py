"""Database engine and session lifecycle management."""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from cryolens.config.settings import get_settings

Base = declarative_base()


@lru_cache(maxsize=4)
def get_db_engine(db_url: str | None = None) -> Engine:
    """Create and cache SQLAlchemy Engine using connection string or default settings."""
    if db_url is None:
        db_url = get_settings().db.url

    return create_engine(
        db_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


def get_db_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Create session factory bound to given or default engine."""
    if engine is None:
        engine = get_db_engine()
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db_session() -> Generator[Session, None, None]:
    """FastAPI dependency for yielding transactional database sessions."""
    session_factory = get_db_session_factory()
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
