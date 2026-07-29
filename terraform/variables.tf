variable "region" {
  type    = string
  default = "us-east-2"
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
