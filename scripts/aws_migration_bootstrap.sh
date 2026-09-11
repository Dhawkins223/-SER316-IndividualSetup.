#!/usr/bin/env bash
set -euo pipefail

PROJECT="hawknetic-sports-tools"
AWS_REGION="${AWS_REGION:-us-east-2}"
REPO="Dhawkins223/HawkNeticSportsTools"

echo "=================================================="
echo " Hawknetic AWS Migration Bootstrap"
echo "=================================================="

for cmd in aws git terraform; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "ERROR: $cmd is required."
    exit 1
  }
done

echo
echo "Checking AWS identity..."
IDENTITY="$(aws sts get-caller-identity --output json)"
ACCOUNT_ID="$(printf '%s' "$IDENTITY" | python -c 'import sys,json; print(json.load(sys.stdin)["Account"])')"
ARN="$(printf '%s' "$IDENTITY" | python -c 'import sys,json; print(json.load(sys.stdin)["Arn"])')"

echo "AWS account: $ACCOUNT_ID"
echo "AWS principal: $ARN"
echo "AWS region: $AWS_REGION"

if printf '%s' "$ARN" | grep -q ':root$'; then
  echo "ERROR: Refusing to run migration bootstrap as the AWS root user."
  echo "Use IAM Identity Center or an administrative role session."
  exit 1
fi

export AWS_REGION
export AWS_DEFAULT_REGION="$AWS_REGION"

echo
echo "Checking repository..."
if [ ! -d "HawkNeticSportsTools/.git" ]; then
  git clone "https://github.com/${REPO}.git"
fi

cd HawkNeticSportsTools
git fetch origin
git checkout aws/migration-foundation
git pull --ff-only origin aws/migration-foundation

mkdir -p   infrastructure/aws/bootstrap   infrastructure/aws/modules/network   infrastructure/aws/modules/ecr   infrastructure/aws/modules/ecs   infrastructure/aws/modules/rds   infrastructure/aws/modules/s3   infrastructure/aws/modules/iam   infrastructure/aws/modules/observability   infrastructure/aws/modules/scheduler   infrastructure/aws/modules/secrets   infrastructure/aws/environments/dev   infrastructure/aws/environments/prod   docs/aws-migration

cat > docs/aws-migration/BOOTSTRAP_STATE.md <<EOF
# AWS Migration Bootstrap

Project: HawkNeticSportsTools
AWS account: ${ACCOUNT_ID}
Region: ${AWS_REGION}

Bootstrap verified AWS identity without provisioning production workloads.

Next gates:
1. AWS security baseline
2. AWS Organizations/member-account resolution
3. provider inventory reconciliation
4. Terraform network/ECR/RDS/ECS plan
5. Railway PostgreSQL recovery
6. data migration
7. AWS shadow deployment
8. cutover only after parity
EOF

echo
echo "Checking Terraform configuration..."
if find infrastructure/aws -name '*.tf' -print -quit | grep -q .; then
  terraform -chdir=infrastructure/aws fmt -recursive
  terraform -chdir=infrastructure/aws init
  terraform -chdir=infrastructure/aws validate
  terraform -chdir=infrastructure/aws plan
else
  echo "No Terraform files yet; no apply attempted."
fi

echo
echo "=================================================="
echo "Bootstrap complete."
echo "NO PRODUCTION AWS RESOURCES WERE APPLIED."
echo "=================================================="
