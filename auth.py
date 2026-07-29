"""
Shared-secret auth. The Function URL is open at the edge (auth NONE), so every
protected endpoint depends on this check. Only callers holding the API_KEY
(the Ocean Blue app, server-side) get through.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from config import get_settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    # Auth disabled (no key configured) — allow. Intended for local dev only.
    if not settings.auth_enabled:
        return
    # Constant-time compare so we don't leak the key length/prefix via timing.
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key.")
