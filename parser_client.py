"""
Client for the existing Resume Extraction Engine (`POST /extract`). Used by
/ingest and the backfill script so we never re-implement parsing — a raw file
goes to the extraction Lambda and comes back as structured ResumeAnalysis.
"""
from __future__ import annotations

from typing import Any

import httpx

from config import get_settings


async def parse_file(file_bytes: bytes, file_name: str, content_type: str) -> dict[str, Any]:
    """Send a resume file to the extraction Lambda and return its structured
    analysis (personal_information stripped — we never keep contact details)."""
    settings = get_settings()
    if not settings.parser_url:
        raise RuntimeError("RESUME_PARSER_URL is not configured.")

    files = {"file": (file_name or "resume", file_bytes, content_type or "application/octet-stream")}
    timeout = httpx.Timeout(settings.parser_timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{settings.parser_url}/extract", files=files)

    if resp.status_code >= 400:
        detail = f"Extraction service returned {resp.status_code}"
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("detail"):
                detail = body["detail"] if isinstance(body["detail"], str) else str(body["detail"])
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(detail)

    data = resp.json()
    if isinstance(data, dict):
        data.pop("personal_information", None)
    return data
