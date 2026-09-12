terraform {
  # 1.11 is the floor for S3 native state locking (use_lockfile), which the
  # environments rely on instead of a DynamoDB lock table.
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.x is where the provider's write-only argument support and the
      # current default_tags behaviour settled. Pinned to the major so a 7.0
      # release cannot land silently in CI.
      version = "~> 6.0"
    }
  }
}
