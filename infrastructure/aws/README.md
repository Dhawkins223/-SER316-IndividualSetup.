# AWS infrastructure workspace

Terraform for the HawkNeticSportsTools AWS migration.

**Nothing in this directory has been applied.** No AWS resources exist. Railway
remains production and the rollback platform.

## Layout

```text
infrastructure/aws/
  bootstrap/              S3 state backend. Applied once, before everything else.
  environments/
    dev/                  Cost-reduced. No NAT, 1 web task, db.t4g.micro.
    prod/                 NAT, 2 web tasks, db.t4g.small, deletion protection.
  modules/
    network/              VPC, three subnet tiers, egress mode switch
    rds/                  PostgreSQL, private, encrypted, storage autoscaling
    ecr/                  Image repository with lifecycle rules
    ecs/                  Cluster, ALB, IAM roles, web service
    scheduler/            Workers: always-on services and scheduled RunTask
    storage/              S3 buckets, encrypted, lifecycle-managed
    secrets/              Secrets Manager containers (no values)
    github-oidc/          GitHub Actions federation, no static keys
    observability/        CloudWatch alarms, SNS, budgets
```

## Status

| Check | Result |
| --- | --- |
| `terraform fmt -check -recursive` | Clean |
| `validate` bootstrap | Success |
| `validate` environments/dev | Success |
| `validate` environments/prod | Success |
| `validate` all 9 modules standalone | All OK |

Terraform **1.13.1**, AWS provider **~> 6.0**. The `>= 1.11.0` floor is real:
the backends use S3 native state locking (`use_lockfile`), which is not
available earlier. It replaces a DynamoDB lock table.

## Gates before any apply

Every one of these must pass. They are not advisory.

1. Authenticated **non-root** AWS session, via IAM Identity Center.
   `aws sts get-caller-identity` succeeds and the ARN is not `:root`.
2. Account security baseline verified: root MFA on, no root access keys, no
   stray IAM users, an Identity Center administrator exists.
3. Target account and region confirmed (`us-east-2`).
4. **Railway PostgreSQL recovered and backed up.** See
   `docs/aws-migration/database-recovery.md`. Migrating from a database that
   will not start is not possible, and migrating from one with no verified
   backup is not sensible.
5. Cost reviewed and a budget approved. See `docs/aws-migration/cost-model.md`
   — the headline is that this costs roughly 5–10× Railway.
6. `terraform plan` read in full. Not skimmed.
7. No unexplained destructive action in the plan.

## Order of operations

```bash
# 1. State backend. Local state on first apply -- it creates its own bucket.
cd infrastructure/aws/bootstrap
terraform init
terraform plan          # read it
terraform apply
terraform output -raw state_bucket_name

# 2. Migrate the bootstrap stack's own state into the bucket it just made,
#    by adding the backend block from `terraform output backend_config_hint`,
#    then:
terraform init -migrate-state

# 3. Dev, end to end, before production is touched.
cd ../environments/dev
cp backend.hcl.example backend.hcl      # fill in the bucket name
terraform init -backend-config=backend.hcl
terraform plan -var="image=..." -var="certificate_arn=..."
terraform apply

# 4. Production, only after dev is proven and parity passes.
cd ../prod
```

## Rules

- **No static AWS access keys.** Not in this repository, not in GitHub secrets,
  not anywhere. Human access is IAM Identity Center; machine access is GitHub
  OIDC. The `github-oidc` module exists so there is never a reason.
- **No secret values in Terraform.** The `secrets` module creates empty
  containers; values are written out of band. RDS generates and holds its own
  master password. Anything in a Terraform variable is in state in plaintext.
- **`terraform apply` only after reading the plan.** Production needs stronger
  controls than dev, not the same ones applied faster.
- **Do not build production in the console** and leave Terraform unaware of it.
  Drift that Terraform cannot see is drift nobody can review.
- **The Route 53 record is not in this configuration**, deliberately. Cutover is
  a separate, owner-approved act rather than a side effect of an apply.

## Things that are structurally impossible here

Worth knowing, because they are the failures this workspace was shaped to
prevent rather than merely discourage:

- **RDS reachable from the internet.** `publicly_accessible` is hardcoded
  `false` and is not a variable.
- **`5432` open to `0.0.0.0/0`.** Database ingress accepts security-group
  references only; there is no CIDR variable to misuse.
- **A deploy role assumable by another repository.** Trust conditions are
  anchored to `repo:<owner>/<repo>:` by the module, and a validation rule
  rejects a bare `*` subject.
- **Storage autoscaling silently disabled.** A validation rule rejects
  `max_allocated_storage == allocated_storage` — the configuration that would
  reproduce the Railway full-volume incident.
