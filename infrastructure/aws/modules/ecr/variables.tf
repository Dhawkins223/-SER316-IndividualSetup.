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
}

variable "tagged_image_count" {
  description = "How many recent tagged images to keep. Each one is a rollback target, so do not set this below a handful."
  type        = number
  default     = 20

  validation {
    condition     = var.tagged_image_count >= 5
    error_message = "Keep at least 5 tagged images so a rollback has somewhere to go."
  }
}

variable "retained_tag_prefixes" {
  description = "Tag prefixes the retention-by-count rule applies to. Must be non-empty: an ECR tagPrefixList rule with no prefixes does not match tagged images."
  type        = list(string)
  default     = ["main", "master", "prod", "sha", "v"]

  validation {
    condition     = length(var.retained_tag_prefixes) > 0
    error_message = "retained_tag_prefixes must not be empty, or the keep-recent-releases rule matches nothing."
  }
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
