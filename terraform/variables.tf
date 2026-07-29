variable "region" {
  type    = string
  default = "us-east-2"
}

variable "environment" {
  type    = string
  default = "production"
}

# ── OpenAI ────────────────────────────────────────────────────────────────────
variable "openai_api_key" {
  type      = string
  sensitive = true
}

variable "openai_model" {
  type    = string
  default = "gpt-4.1-mini"
}

variable "openai_embedding_model" {
  type    = string
  default = "text-embedding-3-small"
}

variable "embedding_dim" {
  type    = number
  default = 1536
}

# ── Vector store ──────────────────────────────────────────────────────────────
variable "use_opensearch" {
  type        = bool
  default     = false
  description = "true = provision OpenSearch Serverless and run the Lambda in k-NN mode; false = DynamoDB brute-force."
}

variable "ddb_table" {
  type    = string
  default = "oceanblue-resume-vectors"
}

variable "ddb_cache_ttl" {
  type    = number
  default = 300
}

variable "opensearch_index" {
  type    = string
  default = "resume-vectors"
}

variable "opensearch_endpoint" {
  type        = string
  default     = ""
  description = "Only used when pointing at a pre-existing collection (use_opensearch=false but backend=opensearch); normally left blank."
}

# ── Matching knobs ────────────────────────────────────────────────────────────
variable "candidate_pool" {
  type    = number
  default = 25
}

variable "rerank_top" {
  type    = number
  default = 10
}

# ── Auth + integrations ───────────────────────────────────────────────────────
variable "api_key" {
  type        = string
  sensitive   = true
  description = "Shared secret the Ocean Blue app sends as X-API-Key."
}

variable "resume_parser_url" {
  type        = string
  description = "Base URL of the existing Resume Extraction Engine Function URL (no trailing slash)."
}

# ── Lambda tuning ─────────────────────────────────────────────────────────────
variable "lambda_memory_mb" {
  type    = number
  default = 1024
}

variable "lambda_reserved_concurrency" {
  type        = number
  default     = -1
  description = "-1 = unreserved. Set a positive cap to bound concurrency (protects OpenAI rate limits / cost)."
}

variable "enable_xray" {
  type        = bool
  default     = false
  description = "Enable AWS X-Ray active tracing on the Lambda."
}

# ── Ops ───────────────────────────────────────────────────────────────────────
variable "ddb_deletion_protection" {
  type        = bool
  default     = true
  description = "Block accidental deletion of the vectors table. Set false in throwaway/dev stacks."
}

variable "allowed_origins" {
  type        = list(string)
  default     = ["*"]
  description = "Function URL CORS origins. Server-to-server calls ignore CORS; tighten if browsers ever call directly."
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "log_level" {
  type    = string
  default = "INFO"
}
