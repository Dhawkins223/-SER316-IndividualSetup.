#!/usr/bin/env bash
set -euo pipefail

PROJECT="hawknetic-sports-tools"
AWS_REGION="${AWS_REGION:-us-east-2}"
REPO="Dhawkins223/HawkNeticSportsTools"
# Optional. Set MIGRATION_BRANCH to pin this run to a specific revision; leave
# it unset to use whatever the checkout is on, or the default branch on a fresh
# clone.
#
# It deliberately has no default. A branch name baked in here goes stale the
# moment the work merges: it named `aws/migration-foundation` while that was
# the migration branch, and that branch still exists without the current
# stacks, so the clone path would fetch a revision the operator never reviewed.
# Replacing it with the next branch name only moves the staleness.
#
# What actually matters is not which branch this is but whether the checkout
# contains the infrastructure about to be validated. require_migration_content
# below checks that, and it stays true after the work merges to the default
# branch -- which a branch-name check cannot.
BRANCH="${MIGRATION_BRANCH:-}"

# The stacks this script validates. Their presence is the real precondition:
# on a checkout without them there is nothing to validate, and proceeding would
# report some other revision's infrastructure as the migration's.
require_migration_content() {
  local missing=""
  for path in \
    infrastructure/aws/bootstrap \
    infrastructure/aws/environments/dev \
    infrastructure/aws/environments/prod \
    infrastructure/aws/modules; do
    [ -d "$path" ] || missing="$missing $path"
  done

  if [ -n "$missing" ]; then
    echo
    echo "ERROR: this checkout does not contain the migration infrastructure."
    echo "Missing:$missing"
    echo
    echo "You are probably on a revision from before the migration work landed."
    echo "Check out the branch or tag carrying it and re-run, for example:"
    echo "  MIGRATION_BRANCH=<branch> $0"
    exit 1
  fi
}

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

  # Never silent about which revision is being validated: the whole failure
  # this guards against is validating one revision while believing it is
  # another.
  CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)"
  echo "Branch: ${CURRENT_BRANCH} ($(git rev-parse --short HEAD 2>/dev/null || echo unknown))"

  # A pin is enforced when one was asked for, and errors rather than checking
  # out: switching branches under someone who may have uncommitted work is a
  # destructive act, and this script exists to verify, not to rearrange a
  # working tree.
  if [ -n "$BRANCH" ] && [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    echo
    echo "ERROR: MIGRATION_BRANCH is '${BRANCH}' but this checkout is on '${CURRENT_BRANCH}'."
    echo
    echo "Either check that branch out and re-run:"
    echo "  git checkout ${BRANCH}"
    echo "or drop the pin to use the checkout as it stands:"
    echo "  MIGRATION_BRANCH= $0"
    exit 1
  fi

  # With no pin, this is what stops an operator on a pre-migration revision
  # validating it and reading the result as the migration's. It holds equally
  # before the work merges and after, which is why it is the check that
  # survives rather than a branch name.
  require_migration_content
else
  echo "Not inside a checkout; cloning $REPO"
  [ -d "HawkNeticSportsTools/.git" ] || git clone "https://github.com/${REPO}.git"
  cd HawkNeticSportsTools
  git fetch origin

  # A fresh clone lands on the default branch, which is correct once the
  # migration work has merged. Before that it is not, so a pin moves off it.
  if [ -n "$BRANCH" ]; then
    git checkout "$BRANCH"
    git pull --ff-only origin "$BRANCH"
  fi

  echo "Branch: $(git rev-parse --abbrev-ref HEAD) ($(git rev-parse --short HEAD))"
  # Same check as the other path, and it is load-bearing here too: cloning a
  # branch that does not carry the stacks succeeds quietly.
  require_migration_content
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
