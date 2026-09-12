# Production environment.
#
# Nothing here is applied until the gates in infrastructure/aws/README.md pass.
# In particular: production traffic stays on Railway until database parity is
# proven, and the DNS record that would move it is not in this configuration at
# all -- cutover is an explicit, separate, owner-approved act.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Filled in by `terraform init -backend-config=backend.hcl` so the bucket
  # name -- which contains the account id -- is not committed. See
  # backend.hcl.example.
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}

locals {
  environment = "prod"
  name_prefix = "hawknetic-prod"

  tags = {
    Project     = "hawknetic"
    Environment = local.environment
    ManagedBy   = "terraform"
    Repository  = var.github_repository
  }

  # Research-only posture, restated in the task definition rather than relying
  # on the image defaults. Both layers is deliberate: the image default stops a
  # forgotten variable coming up permissive, and this makes the running
  # configuration auditable from the console without pulling the image.
  safety_environment = {
    RESEARCH_ONLY                      = "true"
    KALSHI_ORDER_UPLOAD_ENABLED        = "false"
    LIVE_EXECUTION_ENABLED             = "false"
    AUTO_UPLOAD_ENABLED                = "false"
    AUTO_TRADE_ENABLED                 = "false"
    MODEL_PROMOTION_ENABLED            = "false"
    STALE_CACHE_AS_FRESH               = "false"
    DASHBOARD_REQUIRE_AUTH_WHEN_HOSTED = "true"
  }

  common_environment = merge(local.safety_environment, {
    APP_ENV = "production"

    # Ten days of raw payloads. The window sets the steady-state size of
    # raw.source_payloads; at the measured ~166 MB/day that is ~1.7 GB, which
    # RDS storage autoscaling absorbs comfortably. Do not raise this without
    # re-reading docs/raw-payload-retention.md -- an over-long window on a
    # fixed volume is what caused the Railway incident.
    RAW_RETENTION_DAYS = "10"

    # Tell the application what volume ceiling it is running against so its own
    # database_capacity anomaly check reports against the real number.
    DATABASE_VOLUME_CAPACITY_BYTES = tostring(var.rds_max_allocated_storage_gb * 1024 * 1024 * 1024)
  })
}

# --------------------------------------------------------------------------
# Network
# --------------------------------------------------------------------------

module "network" {
  source = "../../modules/network"

  name_prefix = local.name_prefix
  region      = var.region
  cidr_block  = var.vpc_cidr
  az_count    = 2

  # Production pays for NAT: tasks have no public address, which is the
  # conventional and defensible posture for a production workload.
  enable_nat_gateway = true
  single_nat_gateway = var.single_nat_gateway

  # Interface endpoints are only worth their ~$7.30/month each once NAT data
  # processing exceeds that. The free S3 gateway endpoint is always created and
  # already keeps ECR layer pulls off the NAT.
  enable_interface_endpoints = var.enable_interface_endpoints

  tags = local.tags
}

# --------------------------------------------------------------------------
# Registry, storage, secrets
# --------------------------------------------------------------------------

module "ecr" {
  source = "../../modules/ecr"

  repository_name = "hawknetic/app"
  environment     = local.environment
  tags            = local.tags
}

module "storage" {
  source = "../../modules/storage"

  name_prefix = local.name_prefix
  environment = local.environment

  buckets = {
    artifacts = {
      purpose     = "Generated reports, research exports and feature/label CSVs"
      versioned   = true
      expire_days = 365
    }
    "raw-archive" = {
      purpose = "Raw research payload bodies aged out of PostgreSQL"
      # This is the bucket that keeps the database from filling. Payloads are
      # written once and read rarely, so they move to Standard-IA quickly and
      # are kept two years for reproducibility of past research.
      transition_to_ia_days = 30
      expire_days           = 730
    }
    "db-migration" = {
      purpose   = "pg_dump artifacts from the Railway source and RDS restores"
      versioned = true
      # Migration dumps are evidence. They outlive the cutover and the
      # stabilisation window on purpose.
      expire_days = 180
    }
  }

  tags = local.tags
}

module "secrets" {
  source = "../../modules/secrets"

  environment = local.environment

  # Split by workload, not lumped into one document, so a task role can be
  # granted exactly what it needs. The database credential is absent: RDS
  # manages its own master secret.
  secrets = {
    "dashboard-auth"   = { description = "Dashboard authentication password", workload = "web" }
    "kalshi-research"  = { description = "Kalshi API key id and private key", workload = "kalshi-market-ingestion" }
    "external-sources" = { description = "Odds, SportsData and Firecrawl API keys", workload = "external-source-ingestion" }
    "integrations"     = { description = "Optional Airtable and Slack integration credentials", workload = "reporting-evaluation" }
  }

  tags = local.tags
}

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

module "rds" {
  source = "../../modules/rds"

  name_prefix = local.name_prefix
  environment = local.environment

  vpc_id     = module.network.vpc_id
  subnet_ids = module.network.isolated_subnet_ids

  # The only thing allowed to reach 5432. There is no CIDR path.
  allowed_security_group_ids = [module.ecs.task_security_group_id]

  engine_version       = var.rds_engine_version
  engine_major_version = var.rds_engine_major_version
  instance_class       = var.rds_instance_class

  allocated_storage     = var.rds_allocated_storage_gb
  max_allocated_storage = var.rds_max_allocated_storage_gb

  multi_az = var.rds_multi_az

  tags = local.tags
}

# --------------------------------------------------------------------------
# Compute
# --------------------------------------------------------------------------

module "ecs" {
  source = "../../modules/ecs"

  name_prefix = local.name_prefix
  environment = local.environment

  vpc_id                = module.network.vpc_id
  public_subnet_ids     = module.network.public_subnet_ids
  task_subnet_ids       = module.network.task_subnet_ids
  assign_task_public_ip = module.network.tasks_need_public_ip

  image           = var.image
  certificate_arn = var.certificate_arn

  web_cpu           = var.web_cpu
  web_memory        = var.web_memory
  web_desired_count = var.web_desired_count

  common_environment = local.common_environment

  secret_environment = {
    # RDS writes a JSON document with username and password; the :key:: suffix
    # selects one field. The application composes DATABASE_URL from these plus
    # the endpoint below.
    POSTGRES_USER           = "${module.rds.master_user_secret_arn}:username::"
    POSTGRES_PASSWORD       = "${module.rds.master_user_secret_arn}:password::"
    DASHBOARD_AUTH_PASSWORD = "${module.secrets.secret_arns["dashboard-auth"]}:password::"
  }

  secret_arns = [
    module.rds.master_user_secret_arn,
    module.secrets.secret_arns["dashboard-auth"],
  ]

  # The RDS master secret is encrypted with a customer-managed key, so the
  # execution role needs kms:Decrypt on it as well as GetSecretValue.
  secret_kms_key_arns = [module.rds.kms_key_arn]

  task_s3_bucket_arns = [
    module.storage.bucket_arns["artifacts"],
    module.storage.bucket_arns["raw-archive"],
  ]

  log_retention_days = var.log_retention_days

  tags = local.tags
}

# --------------------------------------------------------------------------
# Workers
# --------------------------------------------------------------------------

module "workers" {
  source = "../../modules/scheduler"

  name_prefix = local.name_prefix
  account_id  = data.aws_caller_identity.current.account_id

  cluster_arn            = module.ecs.cluster_arn
  image                  = var.image
  execution_role_arn     = module.ecs.execution_role_arn
  task_role_arn          = module.ecs.task_role_arn
  task_subnet_ids        = module.network.task_subnet_ids
  task_security_group_id = module.ecs.task_security_group_id
  assign_task_public_ip  = module.network.tasks_need_public_ip

  # Cadences mirror worker_services.SERVICE_SPECS exactly. The split is by
  # cadence: anything hourly or slower is scheduled, because holding a
  # container resident for 3,600 or 21,600 seconds to do a few seconds of work
  # is what this migration is supposed to stop paying for.
  workers = {
    # Always-on. 300 s is too frequent for cold starts to be free.
    "kalshi-market-ingestion" = {
      mode     = "service"
      cpu      = 256
      memory   = 512
      use_spot = true
    }
    "external-source-ingestion" = {
      mode     = "service"
      cpu      = 256
      memory   = 512
      use_spot = true
    }
    "crypto-research" = {
      mode     = "service"
      cpu      = 256
      memory   = 512
      use_spot = true
    }

    # Scheduled. Each runs one cycle and exits.
    "sports-research" = {
      mode                = "scheduled"
      schedule            = "cron(5 * * * ? *)"
      cpu                 = 512
      memory              = 1024
      flex_window_minutes = 5
    }
    "research-model-refresh" = {
      mode                = "scheduled"
      schedule            = "cron(20 * * * ? *)"
      cpu                 = 512
      memory              = 1024
      flex_window_minutes = 5
    }
    "settlement-worker" = {
      mode                = "scheduled"
      schedule            = "cron(35 * * * ? *)"
      cpu                 = 256
      memory              = 512
      flex_window_minutes = 5
    }
    # Staggered away from the others: this is the worker that guards storage,
    # and it should not be queued behind a heavy research cycle.
    "raw-retention" = {
      mode                = "scheduled"
      schedule            = "cron(50 * * * ? *)"
      cpu                 = 256
      memory              = 512
      flex_window_minutes = 5
    }
    "reporting-evaluation" = {
      mode                = "scheduled"
      schedule            = "cron(15 */6 * * ? *)"
      cpu                 = 512
      memory              = 1024
      flex_window_minutes = 15
    }
  }

  common_environment = local.common_environment

  secret_environment = {
    POSTGRES_USER     = "${module.rds.master_user_secret_arn}:username::"
    POSTGRES_PASSWORD = "${module.rds.master_user_secret_arn}:password::"
  }

  log_retention_days = var.log_retention_days

  tags = local.tags
}

# --------------------------------------------------------------------------
# Deployment identity
# --------------------------------------------------------------------------

module "github_oidc" {
  source = "../../modules/github-oidc"

  name_prefix       = local.name_prefix
  github_repository = var.github_repository

  roles = {
    # Read-only plan role. Runs on pull requests, so it must not be able to
    # change anything: a plan on an untrusted branch is an untrusted execution.
    plan = {
      description         = "Terraform plan for pull requests. Read-only."
      subjects            = ["pull_request"]
      managed_policy_arns = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
      inline_policy_json = jsonencode({
        Version = "2012-10-17"
        Statement = [{
          # Plan needs to read and lock state. It gets no other write anywhere.
          Effect   = "Allow"
          Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
          Resource = [var.state_bucket_arn, "${var.state_bucket_arn}/*"]
        }]
      })
    }

    # Deploy role, bound to a GitHub environment rather than a branch. A
    # GitHub environment can require reviewers, and the OIDC claim cannot be
    # minted until that gate passes -- so the approval is enforced by GitHub
    # before AWS is ever contacted.
    deploy = {
      description = "Build, push to ECR and update ECS services. Gated on the GitHub 'production' environment."
      subjects    = ["environment:production"]
      inline_policy_json = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Effect   = "Allow"
            Action   = ["ecr:GetAuthorizationToken"]
            Resource = "*"
          },
          {
            Effect = "Allow"
            Action = [
              "ecr:BatchCheckLayerAvailability",
              "ecr:CompleteLayerUpload",
              "ecr:InitiateLayerUpload",
              "ecr:PutImage",
              "ecr:UploadLayerPart",
              "ecr:BatchGetImage",
              "ecr:DescribeImages",
            ]
            Resource = module.ecr.repository_arn
          },
          {
            Effect = "Allow"
            Action = [
              "ecs:DescribeServices",
              "ecs:DescribeTaskDefinition",
              "ecs:RegisterTaskDefinition",
              "ecs:UpdateService",
            ]
            Resource = "*"
          },
          {
            # Registering a task definition means passing the two task roles.
            # Scoped to exactly those two.
            Effect   = "Allow"
            Action   = ["iam:PassRole"]
            Resource = [module.ecs.execution_role_arn, module.ecs.task_role_arn]
            Condition = {
              StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
            }
          },
        ]
      })
    }
  }

  tags = local.tags
}

# --------------------------------------------------------------------------
# Observability and cost
# --------------------------------------------------------------------------

module "observability" {
  source = "../../modules/observability"

  name_prefix = local.name_prefix

  rds_instance_id         = module.rds.instance_id
  alb_arn_suffix          = module.ecs.alb_arn_suffix
  target_group_arn_suffix = module.ecs.target_group_arn_suffix

  # Warn with room to act. The Railway incident had no warning at all.
  rds_free_storage_threshold_gb = var.rds_free_storage_threshold_gb

  alert_email_addresses = var.alert_email_addresses
  monthly_budget_usd    = var.monthly_budget_usd

  tags = local.tags
}
