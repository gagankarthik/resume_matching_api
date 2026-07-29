terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Remote state — create this bucket ONCE manually before first apply:
  #   aws s3api create-bucket --bucket resume-matching-tfstate \
  #     --region us-east-2 --create-bucket-configuration LocationConstraint=us-east-2
  backend "s3" {
    bucket = "resume-matching-tfstate"
    key    = "lambda/terraform.tfstate"
    region = "us-east-2"
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  function_name = "resume-matching-engine"
  # When OpenSearch is enabled, point the Lambda at the created collection.
  opensearch_endpoint = var.use_opensearch ? aws_opensearchserverless_collection.vectors[0].collection_endpoint : var.opensearch_endpoint
  vector_backend      = var.use_opensearch ? "opensearch" : "dynamodb"
}

# ── DynamoDB: resume vectors (always created; the default store) ─────────────

resource "aws_dynamodb_table" "vectors" {
  name         = var.ddb_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "resumeId"

  attribute {
    name = "resumeId"
    type = "S"
  }
}

# ── IAM ─────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "lambda" {
  name = "${local.function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "lambda_dynamodb" {
  name = "lambda-resume-vectors-rw"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:DeleteItem",
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:BatchWriteItem",
      ]
      Resource = [aws_dynamodb_table.vectors.arn, "${aws_dynamodb_table.vectors.arn}/index/*"]
    }]
  })
}

resource "aws_iam_role_policy" "lambda_s3" {
  name = "lambda-read-package-bucket"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "${aws_s3_bucket.packages.arn}/*"
    }]
  })
}

# Data-plane access to the OpenSearch Serverless collection (large mode only).
resource "aws_iam_role_policy" "lambda_aoss" {
  count = var.use_opensearch ? 1 : 0
  name  = "lambda-aoss-access"
  role  = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["aoss:APIAccessAll"]
      Resource = aws_opensearchserverless_collection.vectors[0].arn
    }]
  })
}

# ── S3 bucket for the Lambda zip ─────────────────────────────────────────────

resource "aws_s3_bucket" "packages" {
  bucket = "resume-matching-lambda-packages"
}

resource "aws_s3_bucket_versioning" "packages" {
  bucket = aws_s3_bucket.packages.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "packages" {
  bucket                  = aws_s3_bucket.packages.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# CI uploads lambda.zip to this key before running terraform apply.
resource "aws_s3_object" "zip" {
  bucket = aws_s3_bucket.packages.id
  key    = "lambda.zip"
  source = "${path.module}/../lambda.zip"
  etag   = filemd5("${path.module}/../lambda.zip")
}

# ── Lambda function ──────────────────────────────────────────────────────────

resource "aws_lambda_function" "api" {
  function_name = local.function_name
  role          = aws_iam_role.lambda.arn
  handler       = "handler.handler"
  runtime       = "python3.11"
  timeout       = 300 # /ingest waits on the extraction Lambda (30–90s)
  memory_size   = 1024

  s3_bucket        = aws_s3_bucket.packages.id
  s3_key           = aws_s3_object.zip.key
  source_code_hash = filebase64sha256("${path.module}/../lambda.zip")

  environment {
    variables = {
      OPENAI_API_KEY         = var.openai_api_key
      OPENAI_MODEL           = var.openai_model
      OPENAI_EMBEDDING_MODEL = var.openai_embedding_model
      EMBEDDING_DIM          = tostring(var.embedding_dim)
      VECTOR_BACKEND         = local.vector_backend
      # AWS_REGION is a reserved Lambda env var set automatically to the function
      # region — both boto3 and config.py read it, so we don't (and can't) set it.
      DDB_TABLE              = aws_dynamodb_table.vectors.name
      DDB_CACHE_TTL          = tostring(var.ddb_cache_ttl)
      OPENSEARCH_ENDPOINT    = local.opensearch_endpoint
      OPENSEARCH_INDEX       = var.opensearch_index
      OPENSEARCH_SERVICE     = "aoss"
      MATCH_CANDIDATE_POOL   = tostring(var.candidate_pool)
      MATCH_RERANK_TOP       = tostring(var.rerank_top)
      API_KEY                = var.api_key
      RESUME_PARSER_URL      = var.resume_parser_url
      MAX_FILE_SIZE_MB       = "20"
    }
  }

  depends_on = [aws_cloudwatch_log_group.api]
}

# ── Function URL (no API Gateway) ────────────────────────────────────────────

resource "aws_lambda_function_url" "api" {
  function_name      = aws_lambda_function.api.function_name
  authorization_type = "NONE"

  cors {
    allow_credentials = false
    allow_origins     = ["*"]
    allow_methods     = ["*"]
    allow_headers     = ["*"]
    max_age           = 86400
  }
}

# ── CloudWatch logs ──────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 14
}

# ── OpenSearch Serverless (large mode only) ──────────────────────────────────
# Enable by setting use_opensearch = true. Provides the k-NN vector index for
# banks too large for DynamoDB brute-force.

resource "aws_opensearchserverless_security_policy" "encryption" {
  count = var.use_opensearch ? 1 : 0
  name  = "resume-vectors-enc"
  type  = "encryption"
  policy = jsonencode({
    Rules       = [{ ResourceType = "collection", Resource = ["collection/resume-vectors"] }]
    AWSOwnedKey = true
  })
}

resource "aws_opensearchserverless_security_policy" "network" {
  count = var.use_opensearch ? 1 : 0
  name  = "resume-vectors-net"
  type  = "network"
  policy = jsonencode([{
    Rules = [
      { ResourceType = "collection", Resource = ["collection/resume-vectors"] },
      { ResourceType = "dashboard", Resource = ["collection/resume-vectors"] },
    ]
    AllowFromPublic = true
  }])
}

resource "aws_opensearchserverless_collection" "vectors" {
  count      = var.use_opensearch ? 1 : 0
  name       = "resume-vectors"
  type       = "VECTORSEARCH"
  depends_on = [aws_opensearchserverless_security_policy.encryption]
}

resource "aws_opensearchserverless_access_policy" "data" {
  count = var.use_opensearch ? 1 : 0
  name  = "resume-vectors-data"
  type  = "data"
  policy = jsonencode([{
    Rules = [
      {
        ResourceType = "index"
        Resource     = ["index/resume-vectors/*"]
        Permission   = ["aoss:*"]
      },
      {
        ResourceType = "collection"
        Resource     = ["collection/resume-vectors"]
        Permission   = ["aoss:*"]
      },
    ]
    Principal = [aws_iam_role.lambda.arn]
  }])
}
