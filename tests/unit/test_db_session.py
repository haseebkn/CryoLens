"""Unit tests for database session and engine lifecycle."""

from cryolens.config.settings import DatabaseSettings
from cryolens.db.session import get_db_engine, get_db_session_factory


def test_db_engine_creation() -> None:
    """Verify database engine creation with sqlite memory backend for testing."""
    test_db = DatabaseSettings(
        host="localhost",
        port=5432,
        db="cryolens_test",
        user="test_user",
        password="test_password",
    )
    assert test_db.url == "postgresql://test_user:test_password@localhost:5432/cryolens_test"
    assert (
        test_db.async_url
        == "postgresql+asyncpg://test_user:test_password@localhost:5432/cryolens_test"
    )

    engine = get_db_engine(test_db.url)
    assert engine is not None
    assert str(engine.url) == "postgresql://test_user:***@localhost:5432/cryolens_test"

    session_factory = get_db_session_factory(engine)
    assert session_factory is not None


def test_db_settings_properties() -> None:
    """Verify default database settings."""
    db = DatabaseSettings()
    assert db.host == "localhost"
    assert db.port == 5432
    assert db.db == "cryolens"
    assert db.user == "cryolens_user"
