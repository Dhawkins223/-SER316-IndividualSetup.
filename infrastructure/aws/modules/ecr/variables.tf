variable "repository_name" {
  description = "ECR repository name, e.g. hawknetic/app."
  type        = string
}

variable "environment" {
  description = "Environment name. 'prod' blocks force_delete so a destroy cannot take the rollback images with it."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "image_tag_mutability" {
  description = "IMMUTABLE prevents a tag being repointed at different content, which is what makes a deployed digest auditable. Use MUTABLE only if the pipeline genuinely needs to move a tag such as 'latest'."
  type        = string
  default     = "IMMUTABLE"

  validation {
    condition     = contains(["MUTABLE", "IMMUTABLE"], var.image_tag_mutability)
    error_message = "image_tag_mutability must be MUTABLE or IMMUTABLE."
  }
}

variable "untagged_retention_days" {
  description = "Days to keep untagged images. These are usually layers orphaned by a moved tag and are pure cost."
  type        = number
  default     = 7

  validation {
    # ECR's countNumber must be a positive integer. A fractional or zero value
    # passes Terraform's number type and is only rejected when the lifecycle
    # policy is applied, which is a worse place to find out.
    condition     = var.untagged_retention_days > 0 && floor(var.untagged_retention_days) == var.untagged_retention_days
    error_message = "untagged_retention_days must be a positive whole number; ECR rejects a fractional countNumber at apply."
  }
}

variable "tagged_image_count" {
  description = "How many recent tagged images to keep. Each one is a rollback target, so do not set this below a handful."
  type        = number
  default     = 20

  validation {
    condition     = var.tagged_image_count >= 5 && floor(var.tagged_image_count) == var.tagged_image_count
    error_message = "tagged_image_count must be a whole number of at least 5, so a rollback has somewhere to go and ECR accepts the countNumber."
  }
}

variable "retained_tag_prefixes" {
  description = "Tag prefixes the keep-recent-releases rule applies to. Tagged images outside these prefixes are caught by a lower-priority catch-all rule rather than accumulating forever."
  type        = list(string)
  default     = ["main", "master", "prod", "sha", "v"]

  validation {
    condition     = length(var.retained_tag_prefixes) > 0
    error_message = "retained_tag_prefixes must not be empty, or the keep-recent-releases rule matches nothing."
  }
}

variable "catch_all_tagged_image_count" {
  description = "How many tagged images to keep that match none of retained_tag_prefixes. Without this rule such images match nothing and are never expired, so a stray tag convention quietly accumulates storage."
  type        = number
  default     = 50

  validation {
    condition     = var.catch_all_tagged_image_count >= 1 && floor(var.catch_all_tagged_image_count) == var.catch_all_tagged_image_count
    error_message = "catch_all_tagged_image_count must be a whole number of at least 1."
  }
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
