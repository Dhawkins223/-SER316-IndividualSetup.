#!/usr/bin/env bash
set -euo pipefail

PROJECT="hawknetic-sports-tools"
AWS_REGION="${AWS_REGION:-us-east-2}"
REPO="Dhawkins223/HawkNeticSportsTools"
# The branch carrying the Terraform stacks this script validates. It is not
# `aws/migration-foundation` any more: that was the first migration branch and
# it still exists on the remote, but the current stacks, modules and
# environments live here. Left pointing at the old branch, the clone path below
# would fetch a revision without them and validate something the operator never
# reviewed -- silently, because cloning that branch succeeds.
BRANCH="${MIGRATION_BRANCH:-claude/hawknetic-aws-migration-f5i1l9}"

echo "=================================================="
echo " Hawknetic AWS Migration Bootstrap"
echo "=================================================="

# python3 is checked explicitly: the identity parsing below needs it, and many
# systems (this repository's own sandbox included) have python3 but no `python`.
# Without this the prerequisite check passed and the script then died on the
# first parse with set -e.
for cmd in aws git terraform python3; do
  command -v "$cmd" >/dev/null 2>&1 || {
    echo "ERROR: $cmd is required."
    exit 1
  }
done

echo
echo "Checking AWS identity..."
IDENTITY="$(aws sts get-caller-identity --output json)"
ACCOUNT_ID="$(printf '%s' "$IDENTITY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["Account"])')"
ARN="$(printf '%s' "$IDENTITY" | python3 -c 'import sys,json; print(json.load(sys.stdin)["Arn"])')"

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
# Prefer the checkout this script is part of. Cloning a second copy when the
# operator is already standing in the repository runs some other revision than
# the one they reviewed -- and silently, because the clone succeeds.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
  echo "Using the checkout this script belongs to: $REPO_ROOT"
  cd "$REPO_ROOT"

  # The clone path below checks out "$BRANCH"; this path has to as well, or the
  # preferred path is the unguarded one. An operator standing on Master, or on
  # an older migration branch, would otherwise validate that revision and see
  # it reported as the migration's Terraform -- the same silent
  # wrong-revision failure, reached by the route the script actually
  # recommends.
  #
  # This errors rather than checking out. Switching branches under someone who
  # may have uncommitted work is a destructive act, and this script exists to
  # verify, not to rearrange a working tree.
  CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
  echo "Branch: ${CURRENT_BRANCH} ($(git rev-parse --short HEAD 2>/dev/null || echo unknown))"

  if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    echo
    echo "ERROR: this checkout is on '${CURRENT_BRANCH}', not the migration branch '${BRANCH}'."
    echo "Validating it would report some other revision's infrastructure as the migration's."
    echo
    echo "Either:"
    echo "  git checkout ${BRANCH}"
    echo "and re-run, or set MIGRATION_BRANCH to the branch you actually mean:"
    echo "  MIGRATION_BRANCH=${CURRENT_BRANCH} $0"
    exit 1
  fi
else
  echo "Not inside a checkout; cloning $REPO"
  [ -d "HawkNeticSportsTools/.git" ] || git clone "https://github.com/${REPO}.git"
  cd HawkNeticSportsTools
  git fetch origin
  # A fresh clone lands on the default branch. Without this the script would
  # then validate whatever is on Master rather than the migration branch it
  # exists to bootstrap.
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"
fi

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
# infrastructure/aws is a container directory with no .tf files of its own, so
# initialising and validating it directly always failed. Each stack and module
# is its own root and is validated individually, matching the procedure in
# infrastructure/aws/README.md.
#
# No `terraform plan` here: a plan needs backend configuration and per-
# environment variables, and this script's job is to verify identity and
# configuration, not to reach into state.
if find infrastructure/aws -name '*.tf' -print -quit | grep -q .; then
  terraform -chdir=infrastructure/aws fmt -check -recursive

  TF_FAILED=0
  for dir in \
    infrastructure/aws/bootstrap \
    infrastructure/aws/environments/* \
    infrastructure/aws/modules/*; do
    [ -d "$dir" ] || continue
    find "$dir" -maxdepth 1 -name '*.tf' -print -quit | grep -q . || continue

    printf 'validating %s ... ' "$dir"
    if terraform -chdir="$dir" init -backend=false -input=false >/dev/null 2>&1 \
       && terraform -chdir="$dir" validate -no-color >/dev/null; then
      echo "OK"
    else
      echo "FAILED"
      terraform -chdir="$dir" validate -no-color || true
      TF_FAILED=1
    fi
  done

  [ "$TF_FAILED" -eq 0 ] || {
    echo "ERROR: Terraform validation failed."
    exit 1
  }
else
  echo "No Terraform files yet; nothing to validate."
fi

echo
echo "=================================================="
echo "Bootstrap complete."
echo "NO PRODUCTION AWS RESOURCES WERE APPLIED."
echo "=================================================="
