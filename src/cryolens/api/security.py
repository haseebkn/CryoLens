"""A single named analyst credential for local portfolio review writes."""

import secrets

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from cryolens.config.settings import get_settings

_api_key = APIKeyHeader(name="X-Analyst-Key", auto_error=False)


def require_analyst(key: str | None = Depends(_api_key)) -> str:
    """Fail closed unless a server-configured analyst identity and key both exist."""
    settings = get_settings()
    if not settings.analyst_api_key or not settings.analyst_id.strip():
        raise HTTPException(
            503, "Analyst writes are disabled. Configure a named analyst and API key."
        )
    if key is None or not secrets.compare_digest(key.encode(), settings.analyst_api_key.encode()):
        raise HTTPException(401, "A valid X-Analyst-Key is required.")
    return settings.analyst_id
