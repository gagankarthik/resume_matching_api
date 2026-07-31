"""
One-time / periodic backfill: index the existing resume bank.

Lists resume files under an S3 prefix, downloads each, and POSTs it to the
matching engine's /ingest endpoint (which parses via the extraction Lambda and
stores the vector). Idempotent — re-running overwrites existing vectors, so it's
safe to run again after adding resumes.

Usage:
    python scripts/backfill.py \
        --bucket oceanblue-resumes \
        --prefix resume-bank/ \
        --api-url https://<match-fn-url> \
        --api-key <MATCH_API_KEY> \
        [--limit 100] [--concurrency 3]

Requires: boto3, httpx. AWS credentials from the usual chain (env / profile / role).
The resume_id stored for each file is its S3 key, so it stays stable across runs.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import boto3
import httpx

RESUME_EXTS = (".pdf", ".docx", ".doc", ".txt")


def list_objects(bucket: str, prefix: str, limit: int | None) -> list[dict]:
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    items: list[dict] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            if not key.lower().endswith(RESUME_EXTS):
                continue
            items.append({"key": key, "size": obj["Size"]})
            if limit and len(items) >= limit:
                return items
    return items


def content_type_for(key: str) -> str:
    ext = key.rsplit(".", 1)[-1].lower()
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "doc": "application/msword",
        "txt": "text/plain",
    }.get(ext, "application/octet-stream")


async def ingest_one(
    client: httpx.AsyncClient,
    s3,
    bucket: str,
    api_url: str,
    api_key: str,
    obj: dict,
    sem: asyncio.Semaphore,
    source: str = "bank",
    owner: str | None = None,
) -> tuple[str, bool, str]:
    key = obj["key"]
    async with sem:
        try:
            body = await asyncio.to_thread(
                lambda: s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            )
            file_name = key.rsplit("/", 1)[-1]
            files = {"file": (file_name, body, content_type_for(key))}
            params = {"resume_id": key, "source": source}
            if owner:
                params["owner"] = owner
            resp = await client.post(
                f"{api_url.rstrip('/')}/ingest",
                params=params,
                files=files,
                headers={"X-API-Key": api_key},
            )
            if resp.status_code >= 400:
                return key, "failed", f"{resp.status_code} {resp.text[:200]}"
            skipped = False
            try:
                skipped = bool(resp.json().get("skipped"))
            except Exception:  # noqa: BLE001
                pass
            return key, ("skipped" if skipped else "indexed"), "ok"
        except Exception as exc:  # noqa: BLE001
            return key, "failed", str(exc)


async def main_async(args: argparse.Namespace) -> int:
    objects = list_objects(args.bucket, args.prefix, args.limit)
    if not objects:
        print("No resume files found.")
        return 0
    print(f"Found {len(objects)} resume files. Ingesting (concurrency={args.concurrency})…")

    s3 = boto3.client("s3")
    sem = asyncio.Semaphore(args.concurrency)
    # /ingest parses (30–90s) then embeds — allow a long timeout per file.
    timeout = httpx.Timeout(180.0)
    indexed = skipped = fail = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        tasks = [
            ingest_one(
                client, s3, args.bucket, args.api_url, args.api_key, obj, sem,
                source=args.source, owner=args.owner,
            )
            for obj in objects
        ]
        for coro in asyncio.as_completed(tasks):
            key, status, detail = await coro
            if status == "indexed":
                indexed += 1
                print(f"  ✓ indexed  {key}")
            elif status == "skipped":
                skipped += 1
                print(f"  · skipped  {key} (already indexed)")
            else:
                fail += 1
                print(f"  ✗ failed   {key} — {detail}")

    print(f"\nDone. {indexed} indexed, {skipped} already-indexed, {fail} failed.")
    if fail:
        print("Re-run to retry failures — indexed/skipped resumes won't be re-parsed.")
    return 0 if fail == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Backfill the resume bank into the matching engine.")
    p.add_argument("--bucket", required=True, help="S3 bucket holding resume files")
    p.add_argument("--prefix", default="", help="S3 key prefix (e.g. 'resume-bank/')")
    p.add_argument("--api-url", default=os.getenv("RESUME_MATCH_API_URL", ""), help="Matching engine Function URL")
    p.add_argument("--api-key", default=os.getenv("MATCH_API_KEY", ""), help="Shared X-API-Key")
    p.add_argument("--source", default="bank", help="Tag every ingested resume with this source (scopes /match)")
    p.add_argument("--owner", default=None, help="Tag every ingested resume with this owner (scopes /match per user)")
    p.add_argument("--limit", type=int, default=None, help="Only process the first N files")
    p.add_argument("--concurrency", type=int, default=3, help="Parallel ingests (keep low — each triggers a slow parse)")
    args = p.parse_args()

    if not args.api_url or not args.api_key:
        print("ERROR: --api-url and --api-key (or RESUME_MATCH_API_URL / MATCH_API_KEY env) are required.", file=sys.stderr)
        return 2

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
