"""
Resume Matching Engine — FastAPI app.

Companion to the Resume Extraction Engine. Given a job description it finds the
best-qualifying candidates in the resume bank; given one resume it scores its
fit for a job. Both run on OpenAI embeddings (shortlist) + gpt-4.1-mini (verdict).

Endpoints:
    GET  /            service info
    GET  /health      health check
    POST /embed       store a (already-parsed) resume's vector          [auth]
    POST /ingest      parse a raw file via the extraction Lambda + store [auth]
    POST /match       job description -> ranked candidates               [auth]
    POST /score       one resume vs a job -> fit verdict                 [auth]
    DELETE /vectors/{resume_id}   remove a resume from the store         [auth]
"""
from __future__ import annotations

import logging
import os

import uvicorn
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

import matcher
import parser_client
from auth import require_api_key
from config import get_settings
from models import (
    EmbedRequest,
    EmbedResponse,
    ExistsRequest,
    ExistsResponse,
    MatchRequest,
    MatchResponse,
    ScoreRequest,
    ScoreResponse,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("matching")

settings = get_settings()

app = FastAPI(
    title="Resume Matching Engine",
    description=(
        "Match a job description against a resume bank (best-first ranked "
        "candidates) and score a single resume's fit for a job."
    ),
    version="1.0.0",
)

# CORS is for a local `uvicorn main:app`; the Function URL sets its own in Terraform.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {
        "service": "Resume Matching Engine",
        "version": "1.0.0",
        "embedding_model": settings.embedding_model,
        "chat_model": settings.chat_model,
        "vector_backend": settings.vector_backend,
        "endpoints": {
            "POST /embed": "Store an already-parsed resume's vector",
            "POST /ingest": "Parse a raw file (via extraction Lambda) and store it",
            "POST /match": "Job description -> ranked candidates",
            "POST /score": "One resume vs a job -> fit verdict",
            "DELETE /vectors/{resume_id}": "Remove a resume from the store",
        },
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "vector_backend": settings.vector_backend}


@app.post("/embed", response_model=EmbedResponse, dependencies=[Depends(require_api_key)])
async def embed(req: EmbedRequest):
    if req.analysis is None and not (req.text and req.text.strip()):
        raise HTTPException(status_code=400, detail="Provide `analysis` or `text`.")
    try:
        return await matcher.embed_and_store(req)
    except Exception as exc:  # noqa: BLE001 — surface a clean 500 to the caller
        logger.exception("embed failed")
        raise HTTPException(status_code=500, detail=f"Embed failed: {exc}") from exc


@app.post("/ingest", response_model=EmbedResponse, dependencies=[Depends(require_api_key)])
async def ingest(
    file: UploadFile = File(...),
    resume_id: str | None = Query(default=None, description="Stable id; defaults to the filename"),
    candidate_name: str | None = Query(default=None),
    source: str | None = Query(default="bank"),
    owner: str | None = Query(default=None, description="Who this resume belongs to; scopes later /match calls"),
    force: bool = Query(default=False, description="Re-index even if already present (default skips = idempotent)"),
):
    if not settings.parser_url:
        raise HTTPException(status_code=503, detail="RESUME_PARSER_URL is not configured; /ingest is unavailable.")

    rid = resume_id or (file.filename or "resume")

    # Idempotency: skip BEFORE the expensive parse if this resume is already
    # indexed (so a 400-resume backfill can re-run cheaply / resume after a crash).
    if not force and await matcher.already_indexed(rid):
        return EmbedResponse(resume_id=rid, dim=0, stored=False, skipped=True)

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(file_bytes) > settings.max_file_bytes:
        raise HTTPException(status_code=413, detail=f"File too large (max {settings.max_file_mb} MB).")

    # 1. Parse via the existing extraction engine.
    analysis = await parser_client.parse_file(
        file_bytes,
        file.filename or "resume",
        file.content_type or "application/octet-stream",
    )
    # 2. Embed + store.
    try:
        return await matcher.embed_and_store_analysis(
            resume_id=rid,
            candidate_name=candidate_name,
            analysis=analysis,
            source=source,
            owner=owner,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ingest store failed")
        raise HTTPException(status_code=500, detail=f"Ingest failed: {exc}") from exc


@app.post("/match", response_model=MatchResponse, dependencies=[Depends(require_api_key)])
async def match(req: MatchRequest):
    if req.job is None and not (req.job_text and req.job_text.strip()):
        raise HTTPException(status_code=400, detail="Provide a `job` object or `job_text`.")
    try:
        candidates = await matcher.match_job(
            req.job,
            req.job_text,
            top_k=req.top_k,
            pool=req.pool,
            source=req.source,
            owner=req.owner,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("match failed")
        raise HTTPException(status_code=500, detail=f"Match failed: {exc}") from exc
    return MatchResponse(count=len(candidates), candidates=candidates)


@app.post("/score", response_model=ScoreResponse, dependencies=[Depends(require_api_key)])
async def score(req: ScoreRequest):
    if req.job is None and not (req.job_text and req.job_text.strip()):
        raise HTTPException(status_code=400, detail="Provide a `job` object or `job_text`.")
    if req.resume_id is None and req.analysis is None and not (req.text and req.text.strip()):
        raise HTTPException(status_code=400, detail="Provide `resume_id`, `analysis`, or `text`.")
    try:
        return await matcher.score_one(req)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("score failed")
        raise HTTPException(status_code=500, detail=f"Score failed: {exc}") from exc


@app.get("/vectors/{resume_id}/exists", dependencies=[Depends(require_api_key)])
async def vector_exists(resume_id: str):
    return {"resume_id": resume_id, "exists": await matcher.already_indexed(resume_id)}


@app.post("/vectors/exists", response_model=ExistsResponse, dependencies=[Depends(require_api_key)])
async def vectors_exist(req: ExistsRequest):
    # Batch check — ids in the body so slashes (S3 keys) are safe.
    return ExistsResponse(indexed=await matcher.which_indexed(req.resume_ids))


@app.delete("/vectors/{resume_id}", dependencies=[Depends(require_api_key)])
async def delete_vector(resume_id: str):
    try:
        removed = await matcher.delete_resume(resume_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("delete failed")
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}") from exc
    return {"success": True, "resume_id": resume_id, "removed": removed}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
