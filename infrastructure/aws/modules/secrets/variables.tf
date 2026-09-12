variable "environment" {
  description = "Environment name, used as the secret name prefix (prod/database, dev/database, ...)."
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "secrets" {
  description = <<-EOT
    Secret containers to create, keyed by name. The full name is
    "<environment>/<key>", e.g. "prod/kalshi-research".

    Values are never set here -- see the module header. Write them with:
      aws secretsmanager put-secret-value --secret-id prod/<key> \
        --secret-string file://value.json

    `workload` records which service needs it, so the tag makes the intended
    least-privilege grouping visible.
  EOT
  type = map(object({
    description = string
    workload    = string
  }))
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
