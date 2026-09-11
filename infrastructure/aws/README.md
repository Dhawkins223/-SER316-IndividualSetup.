# AWS migration workspace

This directory is the infrastructure-as-code workspace for the HawkNeticSportsTools AWS migration.

## Safety state

Creating this directory does **not** provision AWS resources.

The active migration branch is `aws/migration-foundation`. Production Railway remains the rollback platform until parity and stabilization pass.

## Planned layout

```text
infrastructure/aws/
  bootstrap/
  modules/
    network/
    ecr/
    ecs/
    rds/
    s3/
    iam/
    observability/
    scheduler/
    secrets/
  environments/
    dev/
    prod/
```

## Provider gate

Before Terraform apply:
- authenticated non-root AWS role/session
- target account and region verified
- budget alerts defined
- production/dev account boundary resolved
- Terraform plan reviewed
- no unexplained destructive action
- Railway database recovery plan approved

## Important

Do not add static AWS access keys to this repository or GitHub Actions. The deployment design should use GitHub OIDC and AWS IAM roles.
