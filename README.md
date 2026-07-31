# Resume Matching Engine

A companion service to the **Resume Extraction Engine**. It answers two questions
on top of the structured resumes that engine produces:

- **Job → candidates** — `POST /match`: given a job description, rank the
  resume bank best-first with a fit score, verdict, and matched/missing skills.
- **Resume → fit** — `POST /score`: does *this one* resume qualify for a job?

Both run the same pipeline: **OpenAI embeddings** (`text-embedding-3-small`) to
shortlist by semantic similarity, then **`gpt-4.1-mini`** to produce a precise,
explained verdict on the shortlist. Same OpenAI account the extraction engine
already uses — no second vendor.

It scales from small to large by flipping one variable (`VECTOR_BACKEND`):
DynamoDB brute-force for small→medium banks, OpenSearch Serverless k-NN for large
ones. See **[ARCHITECTURE.md](./ARCHITECTURE.md)** for the full design.

---

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET`  | `/` | — | Service info |
| `GET`  | `/health` | — | Health check |
| `POST` | `/embed` | ✅ | Store an already-parsed resume's vector (`ResumeAnalysis` JSON) |
| `POST` | `/ingest` | ✅ | Upload a raw file → parse via extraction Lambda → store |
| `POST` | `/match` | ✅ | Job description → ranked candidates |
| `POST` | `/score` | ✅ | One resume vs a job → fit verdict |
| `DELETE` | `/vectors/{resume_id}` | ✅ | Remove a resume from the store |

Auth = an `X-API-Key` header matching the `API_KEY` env var.

### `POST /match`

```jsonc
// request
{
  "job": {
    "title": "Senior Backend Engineer",
    "description": "Build scalable payment APIs …",
    "requirements": ["5+ years Python", "AWS experience"],   // or an HTML string
    "responsibilities": ["Design services", "Mentor engineers"],
    "skills": ["Python", "AWS", "PostgreSQL"]
  },
  "top_k": 10,          // optional
  "source": "truecopy", // optional — only rank resumes stored with this tag
  "owner": "user-123"   // optional — only rank one person's uploads
}
```
```jsonc
// response
{
  "success": true,
  "count": 10,
  "candidates": [
    {
      "resume_id": "resume-bank/jane-doe.pdf",
      "candidate_name": "Jane Doe",
      "fit_score": 91,
      "similarity": 0.83,
      "qualified": true,
      "verdict": "strong",
      "matched_skills": ["Python", "AWS", "PostgreSQL"],
      "missing_skills": ["Kafka"],
      "rationale": "8 years of Python payments experience on AWS matches the core stack."
    }
  ]
}
```

### `POST /score`

```jsonc
// request — pass a stored resume by id, OR inline analysis, OR raw text
{ "job": { "title": "Senior Backend Engineer", "skills": ["Python","AWS"] },
  "resume_id": "resume-bank/jane-doe.pdf" }
```
```jsonc
// response
{ "success": true, "resume_id": "resume-bank/jane-doe.pdf", "fit_score": 88,
  "qualified": true, "verdict": "strong",
  "matched_skills": ["Python","AWS"], "missing_skills": [],
  "rationale": "Meets the core requirements with relevant senior experience." }
```

### `POST /embed`

```jsonc
{ "resume_id": "APP-1042", "candidate_name": "Jane Doe", "source": "application",
  "owner": "user-123",   // optional — who uploaded it
  "analysis": { /* ResumeAnalysis from the extraction engine */ } }
```

---

## Several banks in one table

`source` and `owner` are stored with each resume and accepted as filters on
`/match`. That is what lets unrelated applications share one store: each tags
what it writes, and matches against its own tag.

| Scope on `/match` | What it ranks |
|---|---|
| *(neither)* | every resume in the store |
| `source: "truecopy"` | only what that application uploaded |
| `source: "truecopy"`, `owner: "user-123"` | only that user's uploads |

The filter is applied **before** the shortlist is cut, so a scoped caller gets
a full `top_k` of its own resumes rather than the leftovers of a global
ranking. Resumes stored before scoping existed carry neither tag and so match
no scoped query — to fold the old bank into a scope, re-run the backfill with
`--source`, or `POST /embed` those ids again with the tag set.

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # fill in OPENAI_API_KEY, API_KEY, RESUME_PARSER_URL
uvicorn main:app --reload --port 8001
```

Run the offline tests (no network/AWS needed):

```bash
pytest tests/ -v
```

> Local runs use the DynamoDB backend by default, so you need AWS credentials
> (env/profile) with access to the `oceanblue-resume-vectors` table, or point
> `DDB_TABLE` at a local table. For pure logic testing, the smoke tests avoid
> AWS entirely.

---

## Deploy (AWS, Terraform)

Same shape as the extraction engine: a zip on S3, a Lambda behind a Function URL,
deployed by Terraform via GitHub Actions.

**First-time setup (in order):**
```bash
# 1. See what already exists in the account (read-only, creates nothing):
bash scripts/preflight.sh

# 2. Create the Terraform state bucket (the one resource apply can't create itself).
#    Idempotent + production-hardened (versioning, encryption, TLS-only, no public):
bash terraform/bootstrap.sh
```
Everything else — Lambda, DynamoDB, IAM, Function URL, log group (and OpenSearch
in large mode) — is created by `terraform apply` (via CI on push to `main`).

**GitHub secrets** (Settings → Secrets and variables → Actions):
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `OPENAI_API_KEY`
- `MATCH_API_KEY` — the shared secret the Ocean Blue app will send
- `RESUME_PARSER_URL` — the extraction engine's Function URL
- *(optional vars)* `USE_OPENSEARCH` (`true`/`false`), `OPENAI_MODEL`

Push to `main` → the workflow builds `lambda.zip`, runs `terraform apply`, and
prints the **Function URL**. That URL is what the Ocean Blue app calls.

**Manual deploy** (from `terraform/`):
```bash
terraform init
terraform apply \
  -var="openai_api_key=sk-..." \
  -var="api_key=<long-random>" \
  -var="resume_parser_url=https://<parser-fn-url>"
```

### Going large (OpenSearch)

When the bank outgrows DynamoDB brute-force:
```bash
terraform apply -var="use_opensearch=true" -var=...   # provisions the collection
```
The Lambda switches to `VECTOR_BACKEND=opensearch` automatically, then run the
backfill once to populate the k-NN index.

---

## Populate the bank

Two ways in, both reusing the existing extraction engine:

1. **New applications** — the Ocean Blue app already parses resumes on submit;
   have it `POST /embed` with the `ResumeAnalysis` it gets back (no re-parse).
2. **Existing bank files** — run the backfill, which lists S3, parses each via
   the extraction Lambda (`/ingest`), and stores the vector:

```bash
python scripts/backfill.py \
  --bucket <resume-bank-bucket> \
  --prefix resume-bank/ \
  --api-url https://<match-fn-url> \
  --api-key <MATCH_API_KEY> \
  --concurrency 3
```

Idempotent — the `resume_id` is the S3 key, so re-running overwrites in place.

---

## Wiring it into the Ocean Blue (Next.js) app

The web app only holds a thin server-side client + an API route + UI — no LLM
key in the browser. Add:

- **Env:** `RESUME_MATCH_API_URL` and `RESUME_MATCH_API_KEY` (server-only).
- **Client:** `src/lib/aws/match-candidates.ts` — `matchCandidates(job)` → `POST /match`;
  `scoreResume(job, resumeId|analysis)` → `POST /score`. Mirrors `resume-parser.ts`.
- **Route:** `src/app/api/jobs/[id]/match-candidates/route.ts` — `requireStaff`,
  load the job, call the client, return ranked candidates.
- **On parse:** after `analyzeApplicationResume` succeeds, `POST /embed` the
  analysis so the candidate joins the searchable bank.
- **UI:** a "Best candidates" panel on the job page, a "Job fit" card on the
  application page (cache the `/score` result in a `jobFit` field).

(That Next.js side is built in the Ocean Blue repo, not here.)

---

## Layout

```
resume_matching_api/
├── ARCHITECTURE.md        # AWS design (small→large)
├── main.py                # FastAPI app
├── handler.py             # Mangum Lambda entrypoint
├── config.py              # env settings
├── models.py              # request/response schemas
├── auth.py                # X-API-Key gate
├── text_builder.py        # resume/job → embedding text + summary + skills
├── embedding.py           # OpenAI embeddings (text-embedding-3-small)
├── llm.py                 # gpt-4.1-mini re-rank + single-resume verdict
├── matcher.py             # orchestration
├── parser_client.py       # calls the extraction engine /extract
├── vectorstores/
│   ├── base.py            # VectorStore interface
│   ├── dynamo.py          # brute-force cosine (small→medium)
│   └── opensearch.py      # k-NN (large)
├── scripts/backfill.py    # index the existing bank
├── terraform/             # Lambda + DynamoDB + Function URL + optional OpenSearch
├── tests/test_smoke.py    # offline logic tests
└── .github/workflows/deploy.yml
```
