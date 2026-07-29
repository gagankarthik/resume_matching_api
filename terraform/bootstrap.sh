#!/usr/bin/env bash
#
# One-time bootstrap: create the Terraform remote-state S3 bucket.
#
# Terraform's S3 backend needs this bucket to exist BEFORE `terraform init`.
# Everything else (Lambda, DynamoDB, IAM, Function URL, OpenSearch) is created by
# Terraform itself. This script is idempotent — safe to re-run; it only creates
# what's missing and (re)applies the hardening (versioning, encryption, public
# access block, TLS-only policy).
#
# Usage:  bash terraform/bootstrap.sh
# Requires: authenticated AWS CLI. Honors AWS_REGION (default us-east-2).
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
BUCKET="resume-matching-tfstate"

echo "==> Region: $REGION"
echo "==> State bucket: $BUCKET"

# Verify credentials before doing anything.
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "ERROR: AWS credentials are not valid. Authenticate first (e.g. 'aws login' / 'aws sso login')." >&2
  exit 1
fi
aws sts get-caller-identity --query 'Arn' --output text | sed 's/^/    caller: /'

# Create the bucket if it doesn't exist. head-bucket returns non-zero when
# absent (404) OR when it exists but is owned by someone else (403).
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "==> Bucket already exists — ensuring hardening is applied."
else
  echo "==> Creating bucket $BUCKET ..."
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=$REGION"
  fi
fi

echo "==> Enabling versioning (lets you recover a corrupted/overwritten state) ..."
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

echo "==> Enabling default encryption (AES256) ..."
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

echo "==> Blocking all public access ..."
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

echo "==> Applying TLS-only bucket policy ..."
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "DenyInsecureTransport",
    "Effect": "Deny",
    "Principal": "*",
    "Action": "s3:*",
    "Resource": ["arn:aws:s3:::$BUCKET", "arn:aws:s3:::$BUCKET/*"],
    "Condition": {"Bool": {"aws:SecureTransport": "false"}}
  }]
}
JSON
)"

echo ""
echo "✓ State bucket ready. Next:"
echo "    cd terraform && terraform init"
echo "    terraform apply -var=\"openai_api_key=...\" -var=\"api_key=...\" -var=\"resume_parser_url=...\""
