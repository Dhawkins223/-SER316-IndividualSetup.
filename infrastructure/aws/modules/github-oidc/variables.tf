variable "name_prefix" {
  description = "Prefix applied to role names, e.g. hawknetic-prod."
  type        = string
}

variable "github_repository" {
  description = "The one repository allowed to assume these roles, as owner/repo. Every trust condition is anchored to this value, so a role cannot be assumed from another repository."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be exactly owner/repo, with no wildcards -- a wildcard here would let other repositories assume the role."
  }
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider. It is account-global, so set this false and pass existing_oidc_provider_arn if the account already has one."
  type        = bool
  default     = true
}

variable "existing_oidc_provider_arn" {
  description = "ARN of an existing GitHub OIDC provider, used when create_oidc_provider is false."
  type        = string
  default     = null

  validation {
    condition     = var.create_oidc_provider || var.existing_oidc_provider_arn != null
    error_message = "existing_oidc_provider_arn is required when create_oidc_provider is false."
  }
}

variable "roles" {
  description = <<-EOT
    Roles to create, keyed by short name.

    `subjects` are GitHub OIDC `sub` claim suffixes; the module prefixes each
    with "repo:<github_repository>:". Prefer an environment subject
    ("environment:production") over a branch subject for anything that deploys:
    a GitHub environment can require reviewers, and the claim cannot be minted
    until that gate passes.

    Avoid a bare "*" for any role that can change infrastructure.
  EOT
  type = map(object({
    description         = string
    subjects            = list(string)
    managed_policy_arns = optional(list(string), [])
    inline_policy_json  = optional(string)
    max_session_seconds = optional(number, 3600)
  }))

  validation {
    condition     = alltrue([for r in var.roles : length(r.subjects) > 0])
    error_message = "Every role needs at least one subject; a role with none would have an unsatisfiable trust policy."
  }

  validation {
    condition     = alltrue([for r in var.roles : !contains(r.subjects, "*")])
    error_message = "A bare '*' subject would let any branch, tag, or pull request in the repository assume the role. Name the environment or branch explicitly."
  }
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}
