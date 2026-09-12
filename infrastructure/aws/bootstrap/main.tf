# Terraform remote state backend.
#
# Chicken and egg: this stack creates the bucket that every other stack stores
# its state in, so it cannot itself use a remote backend on first apply. Run it
# once with local state, then migrate its own state into the bucket it just
# created (see README.md in this directory).
#
# No DynamoDB lock table. S3 has supported conditional writes since 2024 and
# Terraform 1.11 exposes them as `use_lockfile = true`, which locks via an
# object in the same bucket. That removes a table, its capacity settings, and
# one more thing to forget to create per environment.
#
# This stack is small and changes almost never. That is deliberate: losing the
# state backend is the one failure that makes every other stack unmanageable,
# so it has the fewest moving parts of anything here.

terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "hawknetic"
      Component   = "terraform-state"
      ManagedBy   = "terraform"
      Environment = "shared"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  # Account id in the name because S3 bucket names are globally unique and
  # "hawknetic-terraform-state" is the kind of name someone else has taken.
  bucket_name = coalesce(var.state_bucket_name, "hawknetic-tfstate-${data.aws_caller_identity.current.account_id}")
}

resource "aws_s3_bucket" "state" {
  bucket = local.bucket_name

  # Never true. Emptying this bucket destroys the record of every resource
  # Terraform manages.
  force_destroy = false

  tags = { Name = local.bucket_name }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning is the recovery path for a corrupted or truncated state file, and
# the reason a bad apply is survivable.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# State files contain every attribute of every resource, including values that
# are sensitive even when no password was ever an input. Old versions are kept
# long enough to recover from a bad apply and no longer.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.state_version_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.state.arn,
        "${aws_s3_bucket.state.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}
