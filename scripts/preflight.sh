#!/usr/bin/env bash
#
# Read-only preflight: inventory every AWS resource this stack uses and report
# what exists vs. what will be created. Creates NOTHING. Run before a first-time
# deploy to see the lay of the land and catch problems (bad creds, wrong region,
# name collisions, unreachable parser) early.
#
# Usage:  bash scripts/preflight.sh
# Requires: authenticated AWS CLI. Honors AWS_REGION (default us-east-2).
set -uo pipefail

REGION="${AWS_REGION:-us-east-2}"
STATE_BUCKET="resume-matching-tfstate"
PKG_BUCKET="resume-matching-lambda-packages"
DDB_TABLE="${DDB_TABLE:-oceanblue-resume-vectors}"
FN_NAME="resume-matching-engine"
ROLE_NAME="resume-matching-engine-role"

green() { printf "  \033[32m● PRESENT\033[0m  %s\n" "$1"; }
gray()  { printf "  \033[90m○ absent \033[0m  %s  (Terraform will create)\n" "$1"; }
warn()  { printf "  \033[33m! \033[0m %s\n" "$1"; }

echo "======================================================================"
echo " Resume Matching Engine — AWS preflight (read-only)"
echo "======================================================================"

# ── 1. Credentials + region ──────────────────────────────────────────────────
echo ""
echo "[1] Identity & region"
if ! IDENT=$(aws sts get-caller-identity --output json 2>&1); then
  warn "AWS credentials invalid/expired: $(echo "$IDENT" | tail -1)"
  echo ""
  echo "  → Authenticate first (e.g. 'aws login' / 'aws sso login'), then re-run."
  exit 1
fi
ACCOUNT=$(echo "$IDENT" | grep -o '"Account": *"[0-9]*"' | grep -o '[0-9]*')
ARN=$(echo "$IDENT" | grep -o '"Arn": *"[^"]*"' | sed 's/.*"Arn": *"//;s/"$//')
echo "    account: $ACCOUNT"
echo "    caller : $ARN"
echo "    region : $REGION"

exists=0
missing=0

check_bucket() {
  if aws s3api head-bucket --bucket "$1" 2>/dev/null; then green "s3://$1"; exists=$((exists+1));
  else gray "s3://$1"; missing=$((missing+1)); fi
}

# ── 2. Terraform state bucket (MUST be bootstrapped manually) ────────────────
echo ""
echo "[2] Terraform state bucket (manual bootstrap — not created by apply)"
if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  green "s3://$STATE_BUCKET"
else
  warn "s3://$STATE_BUCKET is MISSING — run: bash terraform/bootstrap.sh"
fi

# ── 3. Resources Terraform manages ───────────────────────────────────────────
echo ""
echo "[3] Terraform-managed resources"
check_bucket "$PKG_BUCKET"

if aws dynamodb describe-table --table-name "$DDB_TABLE" --region "$REGION" >/dev/null 2>&1; then
  green "dynamodb table: $DDB_TABLE"; exists=$((exists+1))
else
  gray "dynamodb table: $DDB_TABLE"; missing=$((missing+1))
fi

if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  green "iam role: $ROLE_NAME"; exists=$((exists+1))
else
  gray "iam role: $ROLE_NAME"; missing=$((missing+1))
fi

if aws lambda get-function --function-name "$FN_NAME" --region "$REGION" >/dev/null 2>&1; then
  green "lambda: $FN_NAME"; exists=$((exists+1))
  if URL=$(aws lambda get-function-url-config --function-name "$FN_NAME" --region "$REGION" \
            --query 'FunctionUrl' --output text 2>/dev/null); then
    echo "                function URL: $URL"
  fi
else
  gray "lambda: $FN_NAME"; missing=$((missing+1))
fi

# ── 4. Dependency: the extraction engine (parser) ────────────────────────────
echo ""
echo "[4] Dependency — Resume Extraction Engine"
PARSER="${RESUME_PARSER_URL:-}"
if [ -z "$PARSER" ]; then
  warn "RESUME_PARSER_URL not set in this shell — skipping reachability check."
  echo "     (/ingest and backfill need it; /match and /score do not.)"
else
  if curl -fsS --max-time 10 "${PARSER%/}/health" >/dev/null 2>&1; then
    green "parser reachable: ${PARSER%/}/health"
  else
    warn "parser NOT reachable at ${PARSER%/}/health (it may be cold-starting)."
  fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "----------------------------------------------------------------------"
echo " Summary: $exists present, $missing to be created by Terraform."
if aws s3api head-bucket --bucket "$STATE_BUCKET" 2>/dev/null; then
  echo " Next: cd terraform && terraform init && terraform plan"
else
  echo " Next: bash terraform/bootstrap.sh   (create state bucket first)"
fi
echo "----------------------------------------------------------------------"
