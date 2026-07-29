# Resume Matching Engine — AWS Architecture

A companion service to the **Resume Extraction Engine**. Where the extraction
engine turns a resume *file* into structured JSON, this engine answers two
questions on top of that structured data:

1. **Job → candidates** — "Given this job description, who in the resume bank
   qualifies, ranked best-first?"
2. **Resume → fit** — "Does *this one* resume qualify for *this* job, and why?"

Both are the same scoring operation at different fan-out. The design goal is a
single service that runs cheaply for a **small** bank (hundreds–low thousands of
resumes) and scales to a **large** one (tens of thousands+) **without a code
change** — you flip one environment variable.

---

## 1. The core idea: embeddings + shortlist + LLM verdict

Matching a job against every resume with an LLM is too slow and too expensive at
any real scale. So we do it in two stages, the standard retrieval pattern:

```
job description
      │
      ▼
 ┌───────────────┐   text-embedding-3-small     ┌──────────────────┐
 │  build job    │ ───────────────────────────▶ │  job vector      │
 │  text         │        (OpenAI)              │  (1536 floats)   │
 └───────────────┘                              └────────┬─────────┘
                                                         │  nearest-neighbour
                                                         ▼  (cosine)
                                             ┌───────────────────────┐
                                             │  VECTOR STORE          │
                                             │  every resume, as a    │
                                             │  pre-computed vector   │
                                             └───────────┬───────────┘
                                                         │  top ~25 shortlist
                                                         ▼
                                             ┌───────────────────────┐
                                             │  gpt-4.1-mini re-rank  │
                                             │  precise verdict per   │
                                             │  candidate + reasons   │
                                             └───────────┬───────────┘
                                                         ▼
                                        ranked candidates (fit score, verdict,
                                        matched skills, missing skills, reason)
```

- **Embeddings are cheap and pre-computed.** Each resume is embedded **once**
  (when it's parsed, or during a one-time backfill) and stored. Matching a job
  only embeds the *job* (one call), then does math against stored vectors.
- **The LLM only sees the shortlist** (~25 candidates), never the whole bank.
  Cost per match is bounded and flat regardless of bank size.
- **Same engine, both features.** `POST /match` runs the full pipeline for a
  job. `POST /score` skips the search and runs just the LLM-verdict step on one
  resume — that's the "does this resume qualify?" button on an application.

Everything uses **OpenAI** (`text-embedding-3-small` for vectors,
`gpt-4.1-mini` for the verdict) — the same account and key the extraction engine
already uses. No second vendor.

---

## 2. Scaling: one variable, two backends

The only part that scales differently by bank size is the **vector store** —
where resume vectors live and how nearest-neighbour search runs. This is hidden
behind a `VectorStore` interface (`vectorstores/base.py`) with two
implementations, chosen at runtime by `VECTOR_BACKEND`:

### `VECTOR_BACKEND=dynamodb` — default, for small→medium banks

- Vectors live in a DynamoDB table (`oceanblue-resume-vectors`), one item per
  resume, the 1536-dim vector stored as **compact float32 bytes** (~6 KB/item).
- On a match, the Lambda loads the vectors and computes cosine similarity in
  memory with NumPy (a single matrix multiply). To avoid re-reading the whole
  table on every request, warm Lambda invocations **cache the vector matrix in
  memory** with a short TTL (`DDB_CACHE_TTL`, default 5 min).
- **No extra infrastructure** beyond one DynamoDB table. This is the right choice
  until the bank is large enough that a full scan per cache-refresh hurts —
  comfortably into the low tens of thousands of resumes on a 1 GB Lambda.

**Rule of thumb:** stay on DynamoDB up to ~10k–20k resumes. A 1536-dim brute
force over 20k vectors is ~30M floats — a few hundred milliseconds, once per
cache window, not per request.

### `VECTOR_BACKEND=opensearch` — for large banks

- Vectors live in an **Amazon OpenSearch Serverless** collection with a `knn_vector`
  index (HNSW, cosine). Search is approximate nearest-neighbour — sub-linear, so
  it stays fast at 100k+ resumes and never loads the whole set into the Lambda.
- The application code is identical; only the store implementation changes. You
  migrate by: provision the collection (Terraform flag `use_opensearch=true`),
  set `VECTOR_BACKEND=opensearch` + `OPENSEARCH_ENDPOINT`, and re-run the
  backfill once to populate the index.

You do **not** need OpenSearch to start. Ship on DynamoDB; switch the variable
the day the bank outgrows it.

---

## 3. How resumes get into the store

A resume must be **embedded and stored** before it can be matched. Two paths,
both reusing the **existing extraction engine** (`/extract`) — we never
re-implement parsing:

| Path | When | Flow |
|---|---|---|
| **`POST /embed`** | A resume was *already parsed* (e.g. a new application in the Ocean Blue app auto-analyses on submit) | The app sends the `ResumeAnalysis` JSON it already has → we build the embedding text → embed → upsert. No file, no re-parse. |
| **`POST /ingest`** | A raw file that has *not* been parsed (a resume-bank upload) | We forward the file to the extraction Lambda `/extract`, get the `ResumeAnalysis`, then embed + upsert. One call does parse + index. |
| **`scripts/backfill.py`** | One-time / periodic, to index the *existing* bank | Lists resume-bank objects in S3, downloads each, runs it through `/ingest`. Idempotent — safe to re-run. |

```
 new application (already parsed)        resume-bank file (not parsed)
            │                                      │
            │ ResumeAnalysis JSON                  │ PDF/DOCX bytes
            ▼                                      ▼
      POST /embed                            POST /ingest ──▶ extraction Lambda /extract
            │                                      │                    │
            │                                      │◀── ResumeAnalysis ─┘
            └──────────────┬───────────────────────┘
                           ▼
                 build embedding text
                           ▼
              embed (text-embedding-3-small)
                           ▼
                 upsert → VECTOR STORE
```

**What we embed:** not the raw resume text, but a compact, matching-relevant
projection of the structured `ResumeAnalysis` — professional summary, career
level, industry, years of experience, all skill buckets, job titles and
technologies from work history, degrees, and certifications. Job descriptions
are projected the same way (title + description + requirements + responsibilities
+ required skills), so a job vector and a resume vector live in the same space.

---

## 4. AWS resources

Deployed with Terraform (mirrors the extraction engine's setup), region
`us-east-2`.

| Resource | Purpose | Always? |
|---|---|---|
| **Lambda** (`resume-matching-engine`) | The FastAPI app via Mangum. python3.11, 1024 MB, 300s timeout. | ✅ |
| **Lambda Function URL** | Public HTTPS entrypoint (no API Gateway → no 29s cap). Auth via a shared `API_KEY` header checked in-app. | ✅ |
| **DynamoDB table** (`oceanblue-resume-vectors`) | Stores resume vectors + light metadata. PK `resumeId`. On-demand billing. | ✅ (the default store) |
| **S3 bucket** (packages) | Holds the Lambda deployment zip. | ✅ |
| **IAM role** | Lambda: CloudWatch logs, DynamoDB R/W on the table, and (large mode) OpenSearch data access. | ✅ |
| **CloudWatch log group** | Logs, 14-day retention. | ✅ |
| **OpenSearch Serverless collection** + data-access policy | The k-NN vector index for large-scale mode. | ⛔ only when `use_opensearch=true` |

Secrets (`OPENAI_API_KEY`, the shared `API_KEY`, the extraction Lambda URL) are
injected as Lambda environment variables from Terraform variables / CI secrets —
never committed.

---

## 5. Security & access

- The Function URL is `authorization_type = NONE` at the edge (same as the
  parser), but **every mutating and querying endpoint requires an `X-API-Key`
  header** matching the `API_KEY` env var. Only the Ocean Blue app (which holds
  the key server-side) can call it. `GET /health` and `GET /` are open.
- The engine never receives or stores candidate contact details it doesn't need
  — it works from the structured `ResumeAnalysis`, and only keeps a short
  matching summary + skills list + the vector per resume.
- No LLM key ever reaches the browser: the Ocean Blue app calls this Lambda
  **server-side only** (a Next.js API route), exactly like it calls the parser.

---

## 6. Request latency budget

| Endpoint | Work | Typical |
|---|---|---|
| `POST /embed` | 1 embedding call + 1 DynamoDB write | < 1 s |
| `POST /ingest` | parse (30–90 s in the extraction Lambda) + embed + write | 30–90 s |
| `POST /match` | 1 embedding call + vector search + 1 gpt-4.1-mini re-rank | 2–6 s |
| `POST /score` | 1 gpt-4.1-mini verdict | 1–3 s |

The 300s Lambda timeout covers `/ingest`'s dependency on the slow parser; the
matching endpoints are interactive-speed.

---

## 7. Why a separate service (recap)

- **Latency & scale isolation** — vector search + LLM re-rank is heavy and
  bursty; it must not run inside the Next.js request thread on Amplify.
- **Key isolation** — the OpenAI key lives here and in the parser, never in the
  web app's client bundle.
- **Reuses the existing pattern** — this is a sibling of the extraction engine:
  same FastAPI + Mangum + Function URL + Terraform shape, same OpenAI account.
- **The web app stays thin** — it only holds an HTTP client (`match-candidates.ts`),
  an API route, and the "Job fit" / "Best candidates" UI.
