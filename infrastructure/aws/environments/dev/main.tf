# Development environment.
#
# Same modules as production, different economics. The differences are all
# deliberate and all about not paying production prices for a workload nobody
# is depending on:
#
#   no NAT Gateway      tasks run in public subnets with a public IP and no
#                       inbound rules. Saves ~$32/month, which is most of this
#                       environment's bill.
#   one web task        no zero-downtime requirement here.
#   db.t4g.micro        smallest sensible instance.
#   Spot for workers    interruption is free in dev.
#   7-day logs          nobody reads a month of dev logs.
#   no deletion guards  dev should be destroyable and rebuildable on demand.
#
# What is NOT relaxed: the research-only safety flags, the private database,
# and the absence of static credentials. Those are correctness, not cost.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

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
  environment = "dev"
  name_prefix = "hawknetic-dev"

  tags = {
    Project     = "hawknetic"
    Environment = local.environment
    ManagedBy   = "terraform"
    Repository  = var.github_repository
  }

  # Identical to production. A dev environment that is permissive where
  # production is not stops being a rehearsal for it.
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

  # See prod/main.tf and docker-entrypoint.sh: the application reads
  # DATABASE_URL only, and the image composes it from these plus the
  # RDS-managed credential secret.
  database_environment = {
    POSTGRES_HOST    = module.rds.address
    POSTGRES_PORT    = tostring(module.rds.port)
    POSTGRES_DB      = module.rds.database_name
    POSTGRES_SSLMODE = "require"
  }

  common_environment = merge(local.safety_environment, local.database_environment, {
    APP_ENV            = "development"
    RAW_RETENTION_DAYS = "3"

    DATABASE_VOLUME_CAPACITY_BYTES = tostring(var.rds_max_allocated_storage_gb * 1024 * 1024 * 1024)
  })
}

module "network" {
  source = "../../modules/network"

  name_prefix = local.name_prefix
  region      = var.region
  cidr_block  = var.vpc_cidr
  az_count    = 2

  # The cost decision for this environment. Tasks get a public IP and egress
  # through the internet gateway; the task security group has no inbound rules,
  # so nothing can reach them. RDS is still in the isolated tier.
  enable_nat_gateway = false

  tags = local.tags
}

module "ecr" {
  source = "../../modules/ecr"

  repository_name = "hawknetic/app-dev"
  environment     = local.environment

  # Dev churns images and none of them are a rollback target worth keeping.
  untagged_retention_days = 3
  tagged_image_count      = 10

  tags = local.tags
}

module "storage" {
  source = "../../modules/storage"

  name_prefix = local.name_prefix
  environment = local.environment

  buckets = {
    artifacts = {
      purpose     = "Generated reports and research exports"
      expire_days = 30
    }
    "raw-archive" = {
      purpose     = "Raw research payload bodies aged out of PostgreSQL"
      expire_days = 30
    }
  }

  tags = local.tags
}

module "secrets" {
  source = "../../modules/secrets"

  environment = local.environment

  secrets = {
    "dashboard-auth"   = { description = "Dashboard authentication password", workload = "web", keys = ["password"] }
    "kalshi-research"  = { description = "Kalshi API key id and private key", workload = "kalshi-market-ingestion", keys = ["api_key_id", "private_key"] }
    "external-sources" = { description = "Odds, SportsData and Firecrawl API keys", workload = "external-source-ingestion", keys = ["odds_api_key", "sportsdata_api_key", "firecrawl_api_key"] }
  }

  tags = local.tags
}

module "rds" {
  source = "../../modules/rds"

  name_prefix = local.name_prefix
  environment = local.environment

  vpc_id     = module.network.vpc_id
  subnet_ids = module.network.isolated_subnet_ids

  engine_version = var.rds_engine_version
  instance_class = var.rds_instance_class

  allocated_storage     = var.rds_allocated_storage_gb
  max_allocated_storage = var.rds_max_allocated_storage_gb

  multi_az = false

  tags = local.tags
}

module "ecs" {
  source = "../../modules/ecs"

  name_prefix = local.name_prefix
  environment = local.environment

  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids
  task_subnet_ids   = module.network.task_subnet_ids
  # True here, because this environment has no NAT: without a public IP the
  # task cannot reach ECR and never starts.
  assign_task_public_ip = module.network.tasks_need_public_ip

  image           = var.image
  certificate_arn = var.certificate_arn

  web_cpu           = 256
  web_memory        = 512
  web_desired_count = 1

  common_environment = local.common_environment

  secret_environment = {
    POSTGRES_USER           = "${module.rds.master_user_secret_arn}:username::"
    POSTGRES_PASSWORD       = "${module.rds.master_user_secret_arn}:password::"
    DASHBOARD_AUTH_PASSWORD = "${module.secrets.secret_arns["dashboard-auth"]}:password::"
  }

  secret_arns = [
    module.rds.master_user_secret_arn,
    module.secrets.secret_arns["dashboard-auth"],
  ]

  secret_kms_key_arns = [module.rds.kms_key_arn]

  task_s3_bucket_arns = [
    module.storage.bucket_arns["artifacts"],
    module.storage.bucket_arns["raw-archive"],
  ]

  log_retention_days = 7

  tags = local.tags
}

# Database ingress, created here rather than inside the rds module.
#
# The module still accepts allowed_security_group_ids and still refuses CIDRs,
# but wiring it from the environment keeps module.rds from depending on
# module.ecs while module.ecs depends on module.rds. Terraform flattens modules
# into a resource graph and would likely have coped, but "likely" is not a
# property to discover during the first production apply -- and a database
# module that depends on the compute module is backwards layering regardless.
resource "aws_vpc_security_group_ingress_rule" "tasks_to_database" {
  security_group_id            = module.rds.security_group_id
  description                  = "PostgreSQL from the application task security group"
  referenced_security_group_id = module.ecs.task_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

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

  # A reduced set. Dev exists to exercise the wiring -- one always-on collector
  # and one scheduled worker prove both paths without paying for eight.
  workers = {
    "kalshi-market-ingestion" = {
      mode     = "service"
      cpu      = 256
      memory   = 512
      use_spot = true
    }
    "raw-retention" = {
      mode                = "scheduled"
      schedule            = "cron(50 * * * ? *)"
      cpu                 = 256
      memory              = 512
      flex_window_minutes = 15
    }
  }

  common_environment = local.common_environment

  secret_environment = {
    POSTGRES_USER     = "${module.rds.master_user_secret_arn}:username::"
    POSTGRES_PASSWORD = "${module.rds.master_user_secret_arn}:password::"
  }

  log_retention_days = 7

  tags = local.tags
}

module "github_oidc" {
  source = "../../modules/github-oidc"

  name_prefix       = local.name_prefix
  github_repository = var.github_repository

  # The OIDC provider is account-global. If dev and prod share an account, only
  # one stack may create it; the other passes existing_oidc_provider_arn.
  create_oidc_provider       = var.create_oidc_provider
  existing_oidc_provider_arn = var.existing_oidc_provider_arn

  roles = {
    deploy = {
      description = "Build, push and deploy to the dev environment from the default branch."
      subjects    = ["ref:refs/heads/Master", "environment:development"]
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

module "observability" {
  source = "../../modules/observability"

  name_prefix = local.name_prefix

  rds_instance_id         = module.rds.instance_id
  alb_arn_suffix          = module.ecs.alb_arn_suffix
  target_group_arn_suffix = module.ecs.target_group_arn_suffix

  rds_free_storage_threshold_gb = 5
  rds_connection_threshold      = 25

  alert_email_addresses = var.alert_email_addresses
  monthly_budget_usd    = var.monthly_budget_usd

  tags = local.tags
}
